"""Notificaciones al sistema de la empresa.

Cada webhook se firma con HMAC-SHA256 para que el receptor pueda comprobar
que viene de esta API y que no fue modificado:

    firma = HMAC_SHA256(WEBHOOK_SECRET, f"{timestamp}.{body}")

Headers enviados:
    X-Webhook-Event:     tipo de evento
    X-Webhook-Timestamp: segundos Unix (sirve para rechazar reenvíos viejos)
    X-Webhook-Signature: sha256=<firma en hexadecimal>
"""

import hashlib
import hmac
import logging
import time

import httpx

from app.config import get_settings
from app.db import utcnow
from app.models import Verification
from app.schemas import VerificationDetail, WebhookEvent, WebhookEventType

logger = logging.getLogger(__name__)


def sign(body: bytes, timestamp: str, secret: str) -> str:
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def notify(event_type: WebhookEventType, verification: Verification) -> bool:
    """Envía el evento con reintentos. Devuelve True si el receptor respondió 2xx."""
    settings = get_settings()
    if not settings.webhook_url:
        logger.info("WEBHOOK_URL no configurada; no se envía %s", event_type)
        return False

    event = WebhookEvent(
        event=event_type,
        occurred_at=utcnow(),
        verification=VerificationDetail.from_model(verification),
    )
    body = event.model_dump_json().encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Event": event_type,
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": f"sha256={sign(body, timestamp, settings.webhook_secret)}",
    }

    for attempt in range(1, settings.webhook_max_attempts + 1):
        try:
            response = httpx.post(
                settings.webhook_url, content=body, headers=headers,
                timeout=settings.webhook_timeout_seconds,
            )
            if response.is_success:
                logger.info("Webhook %s enviado (verificación %s)", event_type, verification.id)
                return True
            logger.warning("Webhook %s respondió %d (intento %d)", event_type, response.status_code, attempt)
        except httpx.HTTPError as exc:
            logger.warning("Webhook %s falló (intento %d): %s", event_type, attempt, exc)
        if attempt < settings.webhook_max_attempts:
            time.sleep(2 ** attempt)

    logger.error("No se pudo entregar el webhook %s de la verificación %s", event_type, verification.id)
    return False
