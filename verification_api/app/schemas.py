"""Modelos Pydantic de request/response de la API.

Las descripciones y ejemplos de estos modelos son los que aparecen en la
documentación automática (/docs).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Verification, VerificationStatus
from app.services.verification import CheckStatus


class ErrorResponse(BaseModel):
    detail: str = Field(description="Descripción del error, en lenguaje simple.")

    model_config = ConfigDict(json_schema_extra={"examples": [{"detail": "El link expiró. Pedile al vendedor que genere uno nuevo."}]})


class VerificationCreated(BaseModel):
    """Respuesta al crear una sesión de verificación."""

    verification_id: str = Field(description="Identificador de la verificación. Se usa para consultar el estado.")
    request_id: str = Field(description="ID de la solicitud de crédito enviado por la empresa.")
    status: VerificationStatus = Field(description="Estado inicial de la verificación.")
    verification_url: str = Field(description="Link único que se le envía al cliente. Es de un solo uso.")
    expires_at: datetime = Field(description="Fecha y hora (UTC) en la que el link deja de ser válido.")

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "verification_id": "ver_3f9c2a7e5b1d4c0e8a6f2b9d7c1e5a30",
        "request_id": "SOL-20261103-4F2A9C",
        "status": "pending",
        "verification_url": "https://ejemplo.trycloudflare.com/verify/q8Zk3mV0wR2xYb7nP4sT1uLc9dHf6gJa",
        "expires_at": "2026-11-03T18:15:00Z",
    }]})

    @classmethod
    def from_model(cls, v: Verification, verification_url: str) -> "VerificationCreated":
        return cls(
            verification_id=v.id,
            request_id=v.request_id,
            status=v.status,
            verification_url=verification_url,
            expires_at=v.expires_at,
        )


class VerificationResult(BaseModel):
    """Resultado del análisis biométrico."""

    liveness: CheckStatus = Field(description="Resultado de la prueba de vida.")
    liveness_score: float | None = Field(default=None, description="Puntaje de la prueba de vida (0 a 1), si el motor lo informa.")
    face_match: CheckStatus = Field(description="Resultado de la comparación entre la cara del video y la foto de la CI.")
    face_match_score: float | None = Field(default=None, description="Puntaje de similitud (0 a 1), si el motor lo informa.")
    reason: str | None = Field(default=None, description="Motivo de la decisión.")


class DocumentData(BaseModel):
    """Datos que Didit leyó del documento. En sandbox son ficticios."""

    document_type: str | None = None
    issuing_state: str | None = None
    document_number: str | None = None
    full_name: str | None = None
    date_of_birth: str | None = None
    expiration_date: str | None = None


class DocumentCheck(BaseModel):
    """Verificación de autenticidad del documento hecha por Didit.

    Con DIDIT_WORKFLOW_MODE=biometric se hace server-to-server con las fotos que cargó
    el vendedor y no cambia el estado de la verificación: se informa aparte para que
    el vendedor lo evalúe. Con DIDIT_WORKFLOW_MODE=kyc es la cédula que escaneó el
    cliente en la sesión de Didit, y sí forma parte de la decisión final.
    """

    status: Literal["pending", "valid", "suspicious", "error"] = Field(
        description="valid: Didit aprobó el documento. suspicious: lo rechazó o lo mandó a revisión. "
        "pending: todavía no hay resultado. error: no se pudo consultar a Didit.",
    )
    provider_status: str | None = Field(default=None, description="Estado que informó Didit: Approved, Declined o In Review.")
    warnings: list[str] = Field(default_factory=list, description="Avisos de Didit sobre el documento.")
    data: DocumentData | None = Field(default=None, description="Datos leídos del documento.")
    checked_at: datetime | None = None

    @classmethod
    def from_model(cls, v: Verification) -> "DocumentCheck | None":
        if v.document_status is None:
            return None
        status = "pending" if v.document_status == "checking" else v.document_status
        return cls(
            status=status,
            provider_status=v.document_provider_status,
            warnings=v.document_warnings or [],
            data=DocumentData(**v.document_data) if v.document_data else None,
            checked_at=v.document_checked_at,
        )


class VerificationDetail(BaseModel):
    """Estado completo de una verificación."""

    verification_id: str
    request_id: str
    status: VerificationStatus = Field(description="pending, video_received, processing, approved, rejected o expired.")
    created_at: datetime
    expires_at: datetime
    video_received_at: datetime | None = None
    completed_at: datetime | None = None
    result: VerificationResult | None = Field(default=None, description="Presente una vez que se analizó el video.")
    document: DocumentCheck | None = Field(
        default=None,
        description="Verificación del documento en Didit. En modo biometric no cambia `status` (se "
        "informa aparte); en modo kyc es parte de la decisión de Didit. Ausente si está desactivada "
        "o si la sesión terminó sin escanear el documento.",
    )

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "verification_id": "ver_3f9c2a7e5b1d4c0e8a6f2b9d7c1e5a30",
        "request_id": "SOL-20261103-4F2A9C",
        "status": "video_received",
        "created_at": "2026-11-03T18:00:00Z",
        "expires_at": "2026-11-03T18:15:00Z",
        "video_received_at": "2026-11-03T18:03:12Z",
        "completed_at": None,
        "result": {
            "liveness": "pending",
            "liveness_score": None,
            "face_match": "pending",
            "face_match_score": None,
            "reason": "El análisis biométrico todavía no está implementado.",
        },
        "document": {
            "status": "valid",
            "provider_status": "Approved",
            "warnings": [],
            "data": {
                "document_type": "ID_CARD",
                "issuing_state": "PRY",
                "document_number": "4567890",
                "full_name": "Juan Pérez",
                "date_of_birth": "1990-01-15",
                "expiration_date": "2032-11-11",
            },
            "checked_at": "2026-11-03T18:00:09Z",
        },
    }]})

    @classmethod
    def from_model(cls, v: Verification) -> "VerificationDetail":
        result = None
        if v.liveness_status is not None:
            result = VerificationResult(
                liveness=CheckStatus(v.liveness_status),
                liveness_score=v.liveness_score,
                face_match=CheckStatus(v.face_match_status or CheckStatus.PENDING),
                face_match_score=v.face_match_score,
                reason=v.decision_reason,
            )
        return cls(
            verification_id=v.id,
            request_id=v.request_id,
            status=v.status,
            created_at=v.created_at,
            expires_at=v.expires_at,
            video_received_at=v.video_received_at,
            completed_at=v.completed_at,
            result=result,
            document=DocumentCheck.from_model(v),
        )


class VideoUploadResponse(BaseModel):
    """Respuesta a la página de verificación al subir el video."""

    status: VerificationStatus
    message: str

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "status": "video_received",
        "message": "Video enviado correctamente.",
    }]})


WebhookEventType = Literal["verification.video_received", "verification.completed"]


class WebhookEvent(BaseModel):
    """Cuerpo del webhook que se envía al sistema de la empresa.

    Se firma con HMAC-SHA256 sobre `"{timestamp}.{body}"` usando WEBHOOK_SECRET.
    Headers: `X-Webhook-Event`, `X-Webhook-Timestamp`, `X-Webhook-Signature: sha256=<hex>`.
    """

    event: WebhookEventType
    occurred_at: datetime
    verification: VerificationDetail
