"""Modelos de base de datos."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import JSON, Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class VerificationStatus(StrEnum):
    PENDING = "pending"                # Link generado, el cliente todavía no empezó.
    IN_PROGRESS = "in_progress"        # Motor alojado: el cliente está en la página del proveedor.
    VIDEO_RECEIVED = "video_received"  # Motor local: video recibido, resultado todavía pendiente.
    PROCESSING = "processing"          # Motor local: analizando liveness y face match.
    IN_REVIEW = "in_review"            # Motor alojado: el proveedor lo marcó para revisión manual.
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"                # El link o la sesión vencieron sin completarse.


# Estados en los que la verificación ya no cambia.
FINAL_STATUSES = frozenset({VerificationStatus.APPROVED, VerificationStatus.REJECTED, VerificationStatus.EXPIRED})


class Verification(Base):
    """Una sesión de verificación de identidad."""

    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Token del link público. Es distinto del id para que conocer uno no
    # permita deducir el otro.
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    ci_number: Mapped[str] = mapped_column(String(10))
    status: Mapped[VerificationStatus] = mapped_column(
        SAEnum(VerificationStatus, native_enum=False, length=20,
               values_callable=lambda e: [m.value for m in e]),
        default=VerificationStatus.PENDING,
    )

    # Motor con el que se creó la sesión. Se guarda para que cambiar
    # VERIFICATION_PROVIDER no afecte a las sesiones que ya estaban en curso.
    provider: Mapped[str] = mapped_column(String(20), default="stub", server_default="stub")

    # Sesión en el proveedor alojado (por ejemplo, Didit).
    external_session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    external_session_url: Mapped[str | None] = mapped_column(String(512))
    external_status: Mapped[str | None] = mapped_column(String(30))

    # Rutas relativas a STORAGE_DIR.
    # Vacía si no se cargó la foto (es opcional con DIDIT_WORKFLOW_MODE=kyc).
    ci_photo_path: Mapped[str] = mapped_column(String(255))
    ci_back_photo_path: Mapped[str | None] = mapped_column(String(255))
    video_path: Mapped[str | None] = mapped_column(String(255))
    video_mime_type: Mapped[str | None] = mapped_column(String(50))
    selfie_path: Mapped[str | None] = mapped_column(String(255))
    # True si falta descargar algún archivo del proveedor (video o selfie).
    media_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    video_received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Resultado del análisis (se completa en la siguiente etapa).
    liveness_status: Mapped[str | None] = mapped_column(String(20))
    liveness_score: Mapped[float | None] = mapped_column(Float)
    face_match_status: Mapped[str | None] = mapped_column(String(20))
    face_match_score: Mapped[float | None] = mapped_column(Float)
    decision_reason: Mapped[str | None] = mapped_column(String(255))

    # Verificación del documento en Didit (server-to-server, ver document_check.py).
    # None si no se corre. Si no: pending, checking, valid, suspicious o error.
    document_status: Mapped[str | None] = mapped_column(String(20))
    document_provider_status: Mapped[str | None] = mapped_column(String(30))
    document_warnings: Mapped[list | None] = mapped_column(JSON)
    document_data: Mapped[dict | None] = mapped_column(JSON)
    document_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
