"""Servidor simulado del sistema de créditos de la empresa.

Reemplaza al sistema real mientras no esté disponible para pruebas:
- Recibe los datos que carga el agente (desde Streamlit).
- Genera un ID de solicitud de crédito y llama a la API de verificación.
- Recibe los webhooks de la API de verificación y los registra en los logs.

Levantar con:
    uvicorn app.main:app --port 8001 --reload
(desde la carpeta company_mock/)
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    verification_api_url: str = "http://localhost:8000"
    verification_api_key: str = "dev-api-key"
    webhook_secret: str = "dev-webhook-secret"
    # Se rechazan webhooks con un timestamp más viejo que esto (reenvíos).
    webhook_tolerance_seconds: int = 300
    http_timeout_seconds: float = 30.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logger = logging.getLogger("company_mock")

app = FastAPI(
    title="Sistema de créditos (simulado)",
    version="0.1.0",
    description="Simula el sistema de la empresa para probar la integración con la API de verificación.",
)

# Estado en memoria: alcanza para la simulación.
credit_requests: dict[str, dict[str, Any]] = {}   # request_id -> datos de la solicitud
webhook_events: list[dict[str, Any]] = []


# ---------- Modelos ----------

class CreditRequestCreated(BaseModel):
    request_id: str = Field(description="ID de la solicitud de crédito generado por la empresa.")
    verification_id: str
    verification_url: str
    expires_at: datetime
    status: str


class WebhookAck(BaseModel):
    received: bool = True


# ---------- Utilidades ----------

def _new_request_id() -> str:
    return f"SOL-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3).upper()}"


def _api_headers() -> dict[str, str]:
    return {"X-API-Key": settings.verification_api_key}


def _forward_error(response: httpx.Response) -> HTTPException:
    """Propaga el error de la API de verificación con su mismo código."""
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    logger.warning("La API de verificación respondió %d: %s", response.status_code, detail)
    return HTTPException(status_code=response.status_code, detail=detail)


def _verify_signature(body: bytes, timestamp: str | None, signature: str | None) -> None:
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Faltan los headers de firma.")
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(status_code=401, detail="Timestamp inválido.") from None
    if age > settings.webhook_tolerance_seconds:
        raise HTTPException(status_code=401, detail="Webhook demasiado viejo.")

    expected = hmac.new(
        settings.webhook_secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(f"sha256={expected}", signature):
        raise HTTPException(status_code=401, detail="Firma inválida.")


# ---------- Endpoints usados por el formulario del agente ----------

@app.post("/credit-requests", response_model=CreditRequestCreated, tags=["Solicitudes de crédito"])
async def create_credit_request(
    ci_number: Annotated[str, Form(description="Número de CI del cliente.")],
    ci_photo: Annotated[UploadFile | None, File(
        description="Foto del frente de la CI del cliente. Opcional si la API usa DIDIT_WORKFLOW_MODE=kyc.",
    )] = None,
    ci_back_photo: Annotated[UploadFile | None, File(description="Foto del dorso de la CI (opcional).")] = None,
) -> CreditRequestCreated:
    request_id = _new_request_id()
    logger.info("Nueva solicitud de crédito %s (CI %s)", request_id, ci_number)

    files = {}
    if ci_photo is not None and ci_photo.filename:
        files["ci_photo"] = (
            ci_photo.filename, await ci_photo.read(), ci_photo.content_type or "application/octet-stream",
        )
    if ci_back_photo is not None and ci_back_photo.filename:
        files["ci_back_photo"] = (
            ci_back_photo.filename, await ci_back_photo.read(), ci_back_photo.content_type or "application/octet-stream",
        )
    data = {"ci_number": ci_number, "request_id": request_id}

    logger.info("Llamando a la API de verificación para %s", request_id)
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            response = await client.post(
                f"{settings.verification_api_url}/verifications",
                files=files, data=data, headers=_api_headers(),
            )
    except httpx.HTTPError as exc:
        logger.error("No se pudo conectar con la API de verificación: %s", exc)
        raise HTTPException(status_code=502, detail="No se pudo conectar con la API de verificación.") from exc

    if not response.is_success:
        raise _forward_error(response)

    verification = response.json()
    credit_requests[request_id] = {
        "ci_number": ci_number,
        "verification_id": verification["verification_id"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    logger.info("Solicitud %s -> verificación %s", request_id, verification["verification_id"])

    return CreditRequestCreated(
        request_id=request_id,
        verification_id=verification["verification_id"],
        verification_url=verification["verification_url"],
        expires_at=verification["expires_at"],
        status=verification["status"],
    )


@app.get("/credit-requests/{request_id}/verification", tags=["Solicitudes de crédito"])
async def get_credit_request_verification(request_id: str) -> dict[str, Any]:
    """Consulta a la API de verificación el estado de la verificación de una solicitud."""
    stored = credit_requests.get(request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Solicitud de crédito no encontrada.")

    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            response = await client.get(
                f"{settings.verification_api_url}/verifications/{stored['verification_id']}",
                headers=_api_headers(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="No se pudo conectar con la API de verificación.") from exc

    if not response.is_success:
        raise _forward_error(response)
    return response.json()


# ---------- Webhook recibido desde la API de verificación ----------

@app.post("/webhooks/verification", response_model=WebhookAck, tags=["Webhooks"])
async def verification_webhook(
    request: Request,
    x_webhook_event: Annotated[str | None, Header()] = None,
    x_webhook_timestamp: Annotated[str | None, Header()] = None,
    x_webhook_signature: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    body = await request.body()
    _verify_signature(body, x_webhook_timestamp, x_webhook_signature)

    payload = json.loads(body)
    verification = payload.get("verification", {})
    logger.info(
        "Webhook recibido: %s | verificación %s | solicitud %s | estado %s",
        x_webhook_event, verification.get("verification_id"),
        verification.get("request_id"), verification.get("status"),
    )
    logger.debug("Payload del webhook: %s", payload)
    webhook_events.append({"received_at": datetime.now(UTC).isoformat(), **payload})
    return WebhookAck()


@app.get("/webhooks/events", tags=["Webhooks"])
def list_webhook_events() -> list[dict[str, Any]]:
    """Lista los webhooks recibidos desde que arrancó el servidor (útil para la demo)."""
    return webhook_events
