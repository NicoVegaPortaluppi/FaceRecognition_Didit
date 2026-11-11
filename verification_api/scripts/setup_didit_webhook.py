"""Registra (o actualiza) el webhook de Didit apuntando a esta API.

Usa PUBLIC_BASE_URL del .env: el webhook queda en {PUBLIC_BASE_URL}/webhooks/didit.
Guarda el secreto de firma en DIDIT_WEBHOOK_SECRET del .env, sin mostrarlo.

Hay que correrlo de nuevo cada vez que cambia la URL pública (por ejemplo,
con un túnel nuevo de cloudflared) y después reiniciar la API.

Uso (desde verification_api/):
    python -m scripts.setup_didit_webhook
"""

import re
import sys

import httpx

from app.config import BASE_DIR, get_settings

LABEL = "API de verificación de identidad"
EVENTS = ["status.updated"]


def main() -> int:
    settings = get_settings()
    if not settings.didit_api_key:
        print("Falta DIDIT_API_KEY en el .env.")
        return 1
    if not settings.public_base_url.startswith("https://"):
        print(f"PUBLIC_BASE_URL tiene que ser una URL pública con HTTPS (ahora: {settings.public_base_url}).")
        return 1

    target_url = f"{settings.public_base_url}/webhooks/didit"
    base = f"{settings.didit_base_url}/v3/webhook/destinations/"
    headers = {"x-api-key": settings.didit_api_key, "accept": "application/json"}

    with httpx.Client(headers=headers, timeout=20) as client:
        response = client.get(base)
        response.raise_for_status()
        existing = next((d for d in response.json() if d.get("label") == LABEL), None)

        body = {"url": target_url, "webhook_version": "v3", "subscribed_events": EVENTS}
        if existing:
            response = client.patch(f"{base}{existing['uuid']}/", json={**body, "enabled": True})
            action = "actualizado"
        else:
            response = client.post(base, json={"label": LABEL, **body})
            action = "creado"
        if not response.is_success:
            print(f"Didit respondió {response.status_code}: {response.text[:300]}")
            return 1

        destination_uuid = existing["uuid"] if existing else response.json()["uuid"]
        secret = client.get(f"{base}{destination_uuid}/").json()["secret_shared_key"]

    env_path = BASE_DIR / ".env"
    content = env_path.read_text()
    line = f"DIDIT_WEBHOOK_SECRET={secret}"
    if re.search(r"^DIDIT_WEBHOOK_SECRET=.*$", content, flags=re.M):
        content = re.sub(r"^DIDIT_WEBHOOK_SECRET=.*$", lambda _: line, content, flags=re.M)
    else:
        content += f"\n{line}\n"
    env_path.write_text(content)

    print(f"Webhook {action}: {target_url}")
    print(f"Eventos: {', '.join(EVENTS)}")
    print("Secreto guardado en DIDIT_WEBHOOK_SECRET (.env). Reiniciá la API para que lo tome.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
