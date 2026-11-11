"""Procesamiento posterior a la subida del video.

Se ejecuta como tarea en segundo plano para que el cliente reciba la
confirmación de inmediato, sin esperar el análisis ni el webhook.
"""

import logging

from app.config import get_settings
from app.db import SessionLocal, utcnow
from app.models import Verification, VerificationStatus
from app.services import storage, webhook
from app.services.verification import decide, get_provider

logger = logging.getLogger(__name__)


def process_uploaded_video(verification_id: str) -> None:
    with SessionLocal() as db:
        verification = db.get(Verification, verification_id)
        if verification is None:
            logger.error("Verificación %s no encontrada al procesar el video", verification_id)
            return

        webhook.notify("verification.video_received", verification)

        provider = get_provider(get_settings().verification_provider)
        logger.info("Analizando verificación %s con el motor '%s'", verification_id, provider.name)
        verification.status = VerificationStatus.PROCESSING
        db.commit()

        video_path = storage.absolute_path(verification.video_path)
        ci_photo_path = storage.absolute_path(verification.ci_photo_path)
        try:
            liveness = provider.check_liveness(video_path)
            match = provider.face_match(video_path, ci_photo_path)
        except Exception:
            logger.exception("Error analizando la verificación %s", verification_id)
            verification.status = VerificationStatus.VIDEO_RECEIVED
            verification.decision_reason = "Error interno durante el análisis."
            db.commit()
            return

        decision = decide(liveness, match)
        verification.liveness_status = liveness.status.value
        verification.liveness_score = liveness.score
        verification.face_match_status = match.status.value
        verification.face_match_score = match.score
        verification.decision_reason = decision.reason

        if decision.status is None:
            # Sin decisión final: el video queda recibido a la espera de análisis.
            verification.status = VerificationStatus.VIDEO_RECEIVED
            db.commit()
            logger.info("Verificación %s sin decisión final: %s", verification_id, decision.reason)
            return

        verification.status = VerificationStatus(decision.status)
        verification.completed_at = utcnow()
        db.commit()
        logger.info("Verificación %s: %s (%s)", verification_id, decision.status, decision.reason)
        webhook.notify("verification.completed", verification)
