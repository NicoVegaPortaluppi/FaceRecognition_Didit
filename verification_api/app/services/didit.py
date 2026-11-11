"""Integración con Didit (https://docs.didit.me).

Usa una sesión alojada. Según DIDIT_WORKFLOW_MODE:
    biometric  Didit hace la prueba de vida y compara la cara con la foto de la CI
               que cargó el agente, que se le envía como `portrait_image`.
    kyc        El cliente escanea su cédula en la sesión: Didit la lee y verifica,
               hace la prueba de vida y compara la cara con la cédula escaneada. Se
               le manda el número de CI que cargó el agente
               (`expected_details.identification_number`) para que lo compare con
               el de la cédula.
El workflow (método de liveness, umbrales, intentos) se configura en la consola
de Didit.

Endpoints usados:
    POST /v3/session/                       crear sesión
    GET  /v3/session/{id}/decision/         resultado, con links al video y la selfie
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import get_settings
from app.errors import InvalidWebhookSignature, ProviderError
from app.services import document_check
from app.services.hosted import DocumentResult, HostedResult, HostedSession, HostedStatus, MediaFile
from app.services.verification import CheckStatus, FaceMatchResult, LivenessResult

logger = logging.getLogger(__name__)

# Didit rechaza portrait_image de más de 2 MB.
_PORTRAIT_MAX_BYTES = 2 * 1024 * 1024
_WEBHOOK_TOLERANCE_SECONDS = 300

_SESSION_STATUS = {
    "Not Started": HostedStatus.NOT_STARTED,
    "In Progress": HostedStatus.IN_PROGRESS,
    "Resubmitted": HostedStatus.IN_PROGRESS,
    "In Review": HostedStatus.IN_REVIEW,
    "Approved": HostedStatus.APPROVED,
    "Declined": HostedStatus.REJECTED,
    "Abandoned": HostedStatus.EXPIRED,
    "Expired": HostedStatus.EXPIRED,
}

_CHECK_STATUS = {
    "Approved": CheckStatus.PASSED,
    "Declined": CheckStatus.FAILED,
}

_APPROVED_KYC = "Documento, prueba de vida y comparación facial superados."
_DEFAULT_REASONS = {
    HostedStatus.APPROVED: "Prueba de vida y comparación facial superadas.",
    HostedStatus.REJECTED: "La verificación fue rechazada por el proveedor.",
    HostedStatus.IN_REVIEW: "El proveedor marcó la verificación para revisión manual.",
    HostedStatus.EXPIRED: "El cliente no completó la verificación a tiempo.",
}


def _portrait_base64(path: Path) -> str:
    """Devuelve la foto en base64, achicándola si supera los 2 MB."""
    data = path.read_bytes()
    if len(data) > _PORTRAIT_MAX_BYTES:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        quality = 90
        while True:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            data = buffer.getvalue()
            if len(data) <= _PORTRAIT_MAX_BYTES or quality <= 50:
                break
            image.thumbnail((int(image.width * 0.8), int(image.height * 0.8)))
            quality -= 10
        logger.info("Foto de CI reducida a %d bytes para Didit", len(data))
    return base64.b64encode(data).decode()


def _score(value: Any) -> float | None:
    """Didit informa puntajes de 0 a 100; la API los expone de 0 a 1."""
    return round(float(value) / 100, 4) if value is not None else None


_DECIDED = ("Approved", "Declined", "In Review")


def _pick(items: Any) -> dict[str, Any] | None:
    """Elige el resultado que cuenta de una lista de chequeos.

    Didit puede devolver varias entradas por chequeo (por ejemplo, un intento
    "Not Finished" con el video y otro "Approved" con el puntaje). Se usa la
    última con resultado; si ninguna tiene, la última de la lista.
    """
    if not isinstance(items, list) or not items:
        return None
    decided = [item for item in items if item.get("status") in _DECIDED]
    return (decided or items)[-1]


def _first_value(items: Any, key: str) -> str | None:
    """Primer valor no vacío de `key` entre todas las entradas."""
    for item in items if isinstance(items, list) else []:
        if item.get(key):
            return item[key]
    return None


def _error_reasons(*checks: dict[str, Any] | None) -> list[str]:
    reasons = []
    for check in checks:
        for warning in (check or {}).get("warnings") or []:
            if warning.get("log_type") == "error":
                reasons.append(warning.get("short_description") or warning.get("risk", ""))
    return [r for r in reasons if r]


def _document(id_raw: dict[str, Any] | None) -> DocumentResult | None:
    """El resultado del documento escaneado en la sesión, si ya está decidido."""
    if not id_raw or id_raw.get("status") not in _DECIDED:
        return None
    data = {key: id_raw.get(key) for key in document_check.DATA_FIELDS}
    if not data.get("full_name"):
        names = [id_raw.get("first_name"), id_raw.get("last_name")]
        data["full_name"] = " ".join(n for n in names if n) or None
    # En la cédula paraguaya el número de CI puede venir como número personal.
    if not data.get("document_number"):
        data["document_number"] = id_raw.get("personal_number")
    return DocumentResult(
        provider_status=id_raw["status"],
        warnings=[document_check.warning_text(w) for w in id_raw.get("warnings") or []],
        data=data,
    )


def _canonical_v2(body: bytes) -> bytes:
    """JSON canónico que firma Didit en X-Signature-V2."""
    def shorten_floats(value):
        if isinstance(value, dict):
            return {k: shorten_floats(v) for k, v in value.items()}
        if isinstance(value, list):
            return [shorten_floats(v) for v in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    canonical = json.dumps(shorten_floats(json.loads(body)), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical.encode()


class DiditProvider:
    name = "didit"

    def __init__(self) -> None:
        settings = get_settings()
        missing = [n for n in ("didit_api_key", "didit_workflow_id") if not getattr(settings, n)]
        if missing:
            raise ValueError(f"Faltan variables para usar Didit: {', '.join(m.upper() for m in missing)}")
        self._settings = settings
        self._headers = {"x-api-key": settings.didit_api_key, "accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self._settings.didit_base_url}{path}"
        try:
            response = httpx.request(
                method, url, headers=self._headers, timeout=self._settings.didit_timeout_seconds, **kwargs,
            )
        except httpx.HTTPError as exc:
            logger.error("Didit %s %s falló: %s", method, path, exc)
            raise ProviderError() from exc
        if not response.is_success:
            logger.error("Didit %s %s respondió %d: %s", method, path, response.status_code, response.text[:500])
            raise ProviderError()
        return response.json()

    @property
    def _kyc(self) -> bool:
        return self._settings.didit_workflow_mode == "kyc"

    def create_session(
        self, *, verification_id: str, ci_photo_path: Path | None, ci_number: str, callback_url: str,
    ) -> HostedSession:
        payload: dict[str, Any] = {
            "workflow_id": self._settings.didit_workflow_id,
            "vendor_data": verification_id,
            "callback": callback_url,
            "callback_method": "both",
            "language": self._settings.didit_language,
        }
        if self._kyc:
            # La cara se compara con la cédula que escanea el cliente; el número de CI
            # que cargó el agente se compara con el de esa cédula.
            expected = {"identification_number": ci_number}
            if self._settings.didit_expected_id_country:
                expected["id_country"] = self._settings.didit_expected_id_country
            payload["expected_details"] = expected
        else:
            payload["portrait_image"] = _portrait_base64(ci_photo_path)
        data = self._request("POST", "/v3/session/", json=payload)
        logger.info("Sesión de Didit creada: %s (verificación %s)", data["session_id"], verification_id)
        return HostedSession(session_id=data["session_id"], url=data["url"])

    def fetch_result(self, session_id: str) -> HostedResult:
        data = self._request("GET", f"/v3/session/{session_id}/decision/")
        provider_status = data.get("status", "")
        status = _SESSION_STATUS.get(provider_status, HostedStatus.IN_PROGRESS)

        liveness_raw = _pick(data.get("liveness_checks"))
        match_raw = _pick(data.get("face_matches"))
        id_raw = _pick(data.get("id_verifications")) if self._kyc else None

        liveness = LivenessResult(
            status=_CHECK_STATUS.get((liveness_raw or {}).get("status"), CheckStatus.PENDING),
            score=_score((liveness_raw or {}).get("score")),
        )
        face_match = FaceMatchResult(
            status=_CHECK_STATUS.get((match_raw or {}).get("status"), CheckStatus.PENDING),
            score=_score((match_raw or {}).get("score")),
        )

        reasons = _error_reasons(id_raw, liveness_raw, match_raw)
        default = _APPROVED_KYC if self._kyc and status is HostedStatus.APPROVED else _DEFAULT_REASONS.get(status)
        reason = "; ".join(reasons) if reasons else (default or "Verificación en curso.")

        # El video y la selfie pueden venir en entradas distintas.
        media = []
        if video_url := _first_value(data.get("liveness_checks"), "video_url"):
            media.append(MediaFile("video", video_url))
        if selfie_url := _first_value(data.get("liveness_checks"), "reference_image"):
            media.append(MediaFile("selfie", selfie_url))

        return HostedResult(
            status=status, provider_status=provider_status, liveness=liveness,
            face_match=face_match, reason=reason[:255], media=media, raw=data,
            document=_document(id_raw),
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> str:
        secret = self._settings.didit_webhook_secret
        if not secret:
            raise InvalidWebhookSignature("DIDIT_WEBHOOK_SECRET no está configurado.")

        timestamp = headers.get("x-timestamp")
        try:
            if abs(time.time() - int(timestamp or "")) > _WEBHOOK_TOLERANCE_SECONDS:
                raise InvalidWebhookSignature("Webhook demasiado viejo.")
        except ValueError:
            raise InvalidWebhookSignature("Timestamp inválido.") from None

        def matches(signed: bytes, *header_names: str) -> bool:
            expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
            return any(
                headers.get(name) and hmac.compare_digest(expected, headers[name]) for name in header_names
            )

        # Primero la firma sobre el cuerpo crudo; si no, la V2 (JSON canónico).
        valid = matches(body, "x-signature", "x-signature-previous") or matches(
            _canonical_v2(body), "x-signature-v2", "x-signature-v2-previous",
        )
        if not valid:
            raise InvalidWebhookSignature()

        payload = json.loads(body)
        logger.info(
            "Webhook de Didit: %s | sesión %s | estado %s",
            payload.get("webhook_type"), payload.get("session_id"), payload.get("status"),
        )
        return payload.get("session_id", "")

    def download(self, url: str) -> tuple[bytes, str | None]:
        # Los links son prefirmados: no llevan la API key.
        try:
            response = httpx.get(url, timeout=60, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # No se registra la URL: es un link firmado a datos biométricos.
            logger.error("No se pudo descargar un archivo de Didit: %s", type(exc).__name__)
            raise ProviderError() from exc
        return response.content, response.headers.get("content-type")
