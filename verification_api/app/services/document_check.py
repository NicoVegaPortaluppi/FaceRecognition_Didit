"""Verificación del documento con Didit, server-to-server.

Al crear la verificación, en segundo plano, se le mandan a Didit las fotos de la
CI que cargó el agente (frente y, si está, dorso) a su API de ID Verification
(`POST /v3/id-verification/`). Didit las evalúa con su propio sistema (que sea un
documento físico y no una pantalla o una fotocopia, foto reemplazada, datos y
formato) y devuelve Approved, Declined o In Review con sus avisos.

El cliente no hace nada: no se le pide que vuelva a subir la cédula.

El resultado no cambia el estado final de la verificación (que depende de la
prueba de vida y el face match). Se informa aparte, en `document`, para que el
agente lo evalúe:
    valid       Didit aprobó el documento ("Documento validado").
    suspicious  Didit lo rechazó o lo mandó a revisión ("Documento sospechoso").
    pending     Todavía no hay resultado.
    error       No se pudo consultar a Didit después de los reintentos.

En sandbox, Didit no analiza la imagen: devuelve siempre datos ficticios
("Sandbox User", documento SANDBOX-0001).
"""

import logging
import time
from typing import Any

import httpx
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.models import Verification
from app.services import storage

logger = logging.getLogger(__name__)

ENDPOINT = "/v3/id-verification/"

PENDING, CHECKING, VALID, SUSPICIOUS, ERROR = "pending", "checking", "valid", "suspicious", "error"

STATUS = {"Approved": VALID, "Declined": SUSPICIOUS, "In Review": SUSPICIOUS}

# Datos leídos del documento que se guardan y se informan (el resto no se guarda).
DATA_FIELDS = (
    "document_type", "issuing_state", "document_number", "full_name", "date_of_birth", "expiration_date",
)


class _DiditUnavailable(Exception):
    """Didit no respondió o respondió con error: vale la pena reintentar."""


def enabled() -> bool:
    """Activa sólo en modo "biometric": en modo "kyc" el documento lo verifica la sesión."""
    settings = get_settings()
    return (
        settings.didit_document_check
        and settings.didit_workflow_mode == "biometric"
        and bool(settings.didit_api_key)
    )


def warning_text(warning: Any) -> str:
    """Didit devuelve los avisos como objetos; se informa su descripción."""
    if isinstance(warning, dict):
        for key in ("short_description", "description", "long_description", "risk"):
            if warning.get(key):
                return str(warning[key])
    return str(warning)


def _call_didit(verification: Verification) -> dict[str, Any]:
    settings = get_settings()
    files = {"front_image": ("ci_front.jpg", storage.absolute_path(verification.ci_photo_path).read_bytes())}
    if verification.ci_back_photo_path:
        files["back_image"] = ("ci_back.jpg", storage.absolute_path(verification.ci_back_photo_path).read_bytes())
    try:
        response = httpx.post(
            f"{settings.didit_base_url}{ENDPOINT}",
            headers={"x-api-key": settings.didit_api_key, "accept": "application/json"},
            files=files,
            data={"perform_document_liveness": "true", "vendor_data": verification.id},
            timeout=settings.didit_document_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise _DiditUnavailable(type(exc).__name__) from exc
    if not response.is_success:
        logger.error("Didit %s respondió %d: %s", ENDPOINT, response.status_code, response.text[:300])
        raise _DiditUnavailable(f"HTTP {response.status_code}")
    return response.json().get("id_verification") or {}


def _claim(verification_id: str) -> bool:
    """Marca el chequeo como en curso, para que no corra dos veces a la vez."""
    with SessionLocal() as db:
        result = db.execute(
            update(Verification)
            .where(Verification.id == verification_id, Verification.document_status.in_([PENDING, ERROR]))
            .values(document_status=CHECKING)
        )
        db.commit()
        return result.rowcount == 1


def run(verification_id: str) -> None:
    """Verifica el documento de una verificación, con reintentos."""
    if not enabled() or not _claim(verification_id):
        return
    max_attempts = get_settings().didit_document_max_attempts
    with SessionLocal() as db:
        verification = db.get(Verification, verification_id)
        result = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = _call_didit(verification)
                break
            except _DiditUnavailable as exc:
                logger.warning(
                    "Verificación %s: falló la verificación del documento (%s, intento %d de %d)",
                    verification_id, exc, attempt, max_attempts,
                )
                if attempt < max_attempts:
                    time.sleep(2 ** attempt)

        if result is None:
            verification.document_status = ERROR
            db.commit()
            logger.error("Verificación %s: no se pudo verificar el documento", verification_id)
            return

        provider_status = result.get("status") or ""
        verification.document_provider_status = provider_status
        verification.document_status = STATUS.get(provider_status, SUSPICIOUS)
        verification.document_warnings = [warning_text(w) for w in result.get("warnings") or []]
        verification.document_data = {key: result.get(key) for key in DATA_FIELDS}
        verification.document_checked_at = utcnow()
        db.commit()
        logger.info(
            "Verificación %s: documento %s (Didit: %s, %d avisos)",
            verification_id, verification.document_status, provider_status,
            len(verification.document_warnings),
        )


def retry_unfinished() -> None:
    """Al arrancar: reintenta los chequeos que quedaron sin terminar.

    Un chequeo en "checking" al arrancar quedó cortado por un reinicio.
    """
    if not enabled():
        return
    with SessionLocal() as db:
        db.execute(
            update(Verification).where(Verification.document_status == CHECKING).values(document_status=PENDING)
        )
        db.commit()
        ids = list(db.scalars(select(Verification.id).where(Verification.document_status.in_([PENDING, ERROR]))))
    if ids:
        logger.info("Hay %d documentos sin verificar; se reintentan", len(ids))
    for verification_id in ids:
        run(verification_id)
