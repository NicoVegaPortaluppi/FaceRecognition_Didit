"""Gestión de las sesiones de verificación en la base de datos."""

import logging
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.errors import LinkAlreadyUsed, LinkExpired, LinkNotFound, VerificationNotFound
from app.models import FINAL_STATUSES, Verification, VerificationStatus
from app.services import storage
from app.services.hosted import is_hosted

logger = logging.getLogger(__name__)


def build_verification_url(token: str) -> str:
    return f"{get_settings().public_base_url}/verify/{token}"


def _expire_if_needed(db: Session, verification: Verification) -> Verification:
    """Marca como `expired` una sesión pendiente cuyo link ya venció.

    El vencimiento se evalúa al leer la sesión, así no hace falta un proceso
    aparte que recorra la base.
    """
    if verification.status is VerificationStatus.PENDING and utcnow() >= verification.expires_at:
        verification.status = VerificationStatus.EXPIRED
        db.commit()
        logger.info("Verificación %s expirada", verification.id)
    return verification


def find_active_by_request(db: Session, request_id: str) -> Verification | None:
    """Devuelve la sesión pendiente y vigente de una solicitud, si existe."""
    stmt = (
        select(Verification)
        .where(
            Verification.request_id == request_id,
            Verification.status == VerificationStatus.PENDING,
            Verification.expires_at > utcnow(),
        )
        .order_by(Verification.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def create_verification(
    db: Session,
    *,
    request_id: str,
    ci_number: str,
    ci_photo: bytes | None,
    ci_photo_extension: str | None,
    ci_back_photo: bytes | None = None,
    ci_back_photo_extension: str | None = None,
    document_check: bool = False,
) -> Verification:
    verification_id = f"ver_{uuid.uuid4().hex}"
    now = utcnow()
    photo_path = storage.save_ci_photo(verification_id, ci_photo, ci_photo_extension) if ci_photo else ""
    back_path = None
    if ci_back_photo is not None:
        back_path = storage.save_file(verification_id, f"ci_back.{ci_back_photo_extension}", ci_back_photo)

    verification = Verification(
        id=verification_id,
        provider=get_settings().verification_provider,
        token=secrets.token_urlsafe(32),
        request_id=request_id,
        ci_number=ci_number,
        status=VerificationStatus.PENDING,
        ci_photo_path=photo_path,
        ci_back_photo_path=back_path,
        document_status="pending" if document_check else None,
        created_at=now,
        expires_at=now + timedelta(minutes=get_settings().link_expiration_minutes),
    )
    db.add(verification)
    db.commit()
    logger.info("Verificación creada: %s (solicitud %s)", verification_id, request_id)
    return verification


def get_by_id(db: Session, verification_id: str) -> Verification:
    verification = db.get(Verification, verification_id)
    if verification is None:
        raise VerificationNotFound()
    return _expire_if_needed(db, verification)


def get_usable_by_token(db: Session, token: str) -> Verification:
    """Devuelve la sesión del link si el cliente todavía la puede usar.

    Con un motor local el link sirve hasta que se sube el video. Con un motor
    alojado también sirve mientras la sesión del proveedor está en curso, por
    si el cliente cerró la pestaña y vuelve a abrir el link.
    """
    verification = db.scalars(select(Verification).where(Verification.token == token)).first()
    if verification is None:
        raise LinkNotFound()
    _expire_if_needed(db, verification)
    if verification.status is VerificationStatus.EXPIRED:
        raise LinkExpired()

    usable = {VerificationStatus.PENDING}
    if is_hosted(verification.provider):
        usable.add(VerificationStatus.IN_PROGRESS)
    if verification.status not in usable:
        raise LinkAlreadyUsed()
    return verification


def get_by_external_session(db: Session, session_id: str) -> Verification | None:
    return db.scalars(select(Verification).where(Verification.external_session_id == session_id)).first()


def claim_hosted_start(db: Session, verification: Verification) -> bool:
    """Pasa la sesión de `pending` a `in_progress` de forma atómica.

    Devuelve False si otro request ya la había iniciado.
    """
    result = db.execute(
        update(Verification)
        .where(
            Verification.id == verification.id,
            Verification.status == VerificationStatus.PENDING,
            Verification.expires_at > utcnow(),
        )
        .values(status=VerificationStatus.IN_PROGRESS)
    )
    db.commit()
    db.refresh(verification)
    return result.rowcount == 1


def claim_final_status(db: Session, verification: Verification, status: VerificationStatus) -> bool:
    """Registra el estado final una sola vez.

    El webhook del proveedor y el regreso del cliente pueden llegar al mismo
    tiempo; solo el primero que llegue cierra la verificación y avisa a la
    empresa.
    """
    result = db.execute(
        update(Verification)
        .where(Verification.id == verification.id, Verification.status.not_in(FINAL_STATUSES))
        .values(status=status, completed_at=utcnow())
    )
    db.commit()
    db.refresh(verification)
    return result.rowcount == 1


def mark_video_received(db: Session, verification: Verification, video_path: str, mime_type: str) -> bool:
    """Registra el video y consume el link.

    El UPDATE condicionado a `status = pending` hace que, si llegan dos
    subidas al mismo tiempo, solo una gane. Devuelve False si se perdió.
    """
    now = utcnow()
    result = db.execute(
        update(Verification)
        .where(
            Verification.id == verification.id,
            Verification.status == VerificationStatus.PENDING,
            Verification.expires_at > now,
        )
        .values(
            status=VerificationStatus.VIDEO_RECEIVED,
            video_path=video_path,
            video_mime_type=mime_type,
            video_received_at=now,
        )
    )
    db.commit()
    db.refresh(verification)
    return result.rowcount == 1
