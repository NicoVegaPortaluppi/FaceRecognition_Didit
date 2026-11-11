"""Flujo de verificación con un motor alojado (por ejemplo, Didit).

    cliente abre nuestro link ─► start_session() ─► redirect a la página del proveedor
    proveedor termina ─► webhook y/o regreso del cliente ─► sync_result()
    sync_result() ─► guarda resultado, avisa a la empresa, descarga video y selfie
    descarga fallida ─► media_pending ─► retry_pending_media() más tarde
"""

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.errors import LinkAlreadyUsed, ProviderError, UnsupportedFileType
from app.models import FINAL_STATUSES, Verification, VerificationStatus
from app.services import document_check, sessions, storage, webhook
from app.services.hosted import HostedResult, HostedStatus, HostedVerificationProvider, MediaFile, get_hosted_provider
from app.services.validation import detect_image, detect_video

logger = logging.getLogger(__name__)

_FINAL = {
    HostedStatus.APPROVED: VerificationStatus.APPROVED,
    HostedStatus.REJECTED: VerificationStatus.REJECTED,
    HostedStatus.EXPIRED: VerificationStatus.EXPIRED,
}


def completion_url() -> str:
    """URL a la que el proveedor devuelve al cliente al terminar.

    No lleva el token del link: el proveedor agrega su propio ID de sesión.
    """
    return f"{get_settings().public_base_url}/verify/complete"


def start_session(db: Session, verification: Verification) -> str:
    """Crea (o reutiliza) la sesión en el proveedor y devuelve su URL."""
    if verification.external_session_url:
        logger.info("Verificación %s: se reutiliza la sesión del proveedor", verification.id)
        return verification.external_session_url

    if not sessions.claim_hosted_start(db, verification):
        # Otro request la está iniciando en este mismo momento.
        if verification.external_session_url:
            return verification.external_session_url
        raise LinkAlreadyUsed()

    provider = get_hosted_provider(verification.provider)
    try:
        hosted = provider.create_session(
            verification_id=verification.id,
            ci_photo_path=storage.absolute_path(verification.ci_photo_path) if verification.ci_photo_path else None,
            ci_number=verification.ci_number,
            callback_url=completion_url(),
        )
    except ProviderError:
        # Se vuelve a pending para que el cliente pueda reintentar.
        verification.status = VerificationStatus.PENDING
        db.commit()
        raise

    verification.external_session_id = hosted.session_id
    verification.external_session_url = hosted.url
    db.commit()
    return hosted.url


def sync_result(verification_id: str) -> None:
    """Consulta el resultado en el proveedor y actualiza la verificación.

    Se puede llamar varias veces (webhook, regreso del cliente, consulta de
    la empresa): solo la primera que encuentre un estado final cierra la
    verificación y avisa a la empresa.
    """
    with SessionLocal() as db:
        verification = db.get(Verification, verification_id)
        if verification is None or not verification.external_session_id:
            return
        if verification.status in FINAL_STATUSES:
            return

        provider = get_hosted_provider(verification.provider)
        try:
            result = provider.fetch_result(verification.external_session_id)
        except ProviderError:
            logger.warning("No se pudo consultar el resultado de la verificación %s", verification_id)
            return

        _store_result(verification, result)
        logger.info(
            "Verificación %s: estado en el proveedor '%s'", verification_id, result.provider_status,
        )

        if result.status is HostedStatus.IN_REVIEW:
            verification.status = VerificationStatus.IN_REVIEW
            db.commit()
            return
        if result.status not in _FINAL:
            db.commit()
            return

        # Si hay archivos para descargar, quedan pendientes hasta que se bajen.
        verification.media_pending = get_settings().didit_download_media and bool(result.media)
        db.commit()
        if not sessions.claim_final_status(db, verification, _FINAL[result.status]):
            return  # Otro proceso ya la cerró.
        logger.info("Verificación %s: %s (%s)", verification_id, verification.status, result.reason)

        # Se avisa antes de descargar: la empresa no tiene que esperar los reintentos.
        webhook.notify("verification.completed", verification)

        if verification.media_pending:
            _download_media(db, verification, result.media, provider)


def retry_pending_media(verification_id: str) -> None:
    """Reintenta descargar los archivos que quedaron pendientes.

    Pide la decisión de nuevo para obtener links frescos, porque los
    anteriores pueden haber vencido.
    """
    with SessionLocal() as db:
        verification = db.get(Verification, verification_id)
        if verification is None or not verification.media_pending:
            return
        try:
            provider = get_hosted_provider(verification.provider)
        except ValueError as exc:
            logger.warning("Verificación %s: no se puede reintentar la descarga (%s)", verification_id, exc)
            return
        try:
            result = provider.fetch_result(verification.external_session_id)
        except ProviderError:
            logger.warning("Verificación %s: no se pudieron obtener links nuevos", verification_id)
            return
        logger.info("Verificación %s: reintentando descarga de archivos pendientes", verification_id)
        _download_media(db, verification, result.media, provider)


def pending_media_ids() -> list[str]:
    with SessionLocal() as db:
        return list(db.scalars(select(Verification.id).where(Verification.media_pending.is_(True))))


def retry_all_pending_media() -> None:
    """Reintenta todas las descargas pendientes (se usa al arrancar la API)."""
    ids = pending_media_ids()
    if ids:
        logger.info("Hay %d verificaciones con archivos pendientes de descarga", len(ids))
    for verification_id in ids:
        retry_pending_media(verification_id)


def _store_result(verification: Verification, result: HostedResult) -> None:
    verification.external_status = result.provider_status
    verification.liveness_status = result.liveness.status.value
    verification.liveness_score = result.liveness.score
    verification.face_match_status = result.face_match.status.value
    verification.face_match_score = result.face_match.score
    verification.decision_reason = result.reason
    if result.document is not None:
        # Modo "kyc": el documento que escaneó el cliente en la sesión.
        document = result.document
        verification.document_provider_status = document.provider_status
        verification.document_status = document_check.STATUS.get(document.provider_status, document_check.SUSPICIOUS)
        verification.document_warnings = document.warnings
        verification.document_data = document.data
        verification.document_checked_at = verification.document_checked_at or utcnow()
    elif (
        verification.document_status == document_check.PENDING
        and get_settings().didit_workflow_mode == "kyc"
        and result.status in _FINAL
    ):
        # La sesión terminó sin resultado del documento (por ejemplo, venció).
        verification.document_status = None


def _already_saved(verification: Verification, kind: str) -> bool:
    return bool(verification.video_path if kind == "video" else verification.selfie_path)


def _download_with_retries(
    verification: Verification, media: MediaFile, provider: HostedVerificationProvider,
) -> bytes | None:
    """Descarga un archivo con reintentos y espera creciente (2 s, 4 s, ...).

    Desde el segundo intento pide la decisión de nuevo para usar un link
    fresco, por si el anterior venció.
    """
    max_attempts = get_settings().didit_media_max_attempts
    url = media.url
    for attempt in range(1, max_attempts + 1):
        try:
            data, _ = provider.download(url)
            return data
        except ProviderError:
            logger.warning(
                "Verificación %s: falló la descarga de '%s' (intento %d de %d)",
                verification.id, media.kind, attempt, max_attempts,
            )
        if attempt == max_attempts:
            break
        time.sleep(2 ** attempt)
        try:
            fresh = provider.fetch_result(verification.external_session_id)
            url = next((m.url for m in fresh.media if m.kind == media.kind), url)
        except ProviderError:
            pass  # Se reintenta con el mismo link.
    return None


def _save_media(verification: Verification, kind: str, data: bytes) -> None:
    try:
        if kind == "video":
            file_kind = detect_video(data)
            verification.video_path = storage.save_file(verification.id, f"video.{file_kind.extension}", data)
            verification.video_mime_type = file_kind.mime_type
            verification.video_received_at = utcnow()
        else:
            file_kind = detect_image(data)
            verification.selfie_path = storage.save_file(verification.id, f"selfie.{file_kind.extension}", data)
    except UnsupportedFileType:
        # Formato inesperado: se guarda igual para no perderlo y no se reintenta.
        name = f"{kind}.bin"
        path = storage.save_file(verification.id, name, data)
        if kind == "video":
            verification.video_path = path
        else:
            verification.selfie_path = path
        logger.warning("Archivo '%s' con formato no reconocido, guardado como %s", kind, name)


def _download_media(
    db: Session, verification: Verification, media_files: list[MediaFile], provider: HostedVerificationProvider,
) -> None:
    """Descarga el video y la selfie que falten.

    Si alguno no se puede bajar, la verificación queda con `media_pending`
    para reintentarlo más tarde.
    """
    missing = False
    for media in media_files:
        if _already_saved(verification, media.kind):
            continue
        data = _download_with_retries(verification, media, provider)
        if data is None:
            missing = True
            continue
        _save_media(verification, media.kind, data)
        db.commit()

    verification.media_pending = missing
    db.commit()
    if missing:
        logger.error(
            "Verificación %s: quedan archivos sin descargar; se reintentará al consultar el estado "
            "o al reiniciar la API", verification.id,
        )
