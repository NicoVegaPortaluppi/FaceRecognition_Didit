"""Páginas que abre el cliente desde su celular.

Según el motor de la verificación:
- Motor local (stub, deepface): el cliente graba el video en nuestra página.
- Motor alojado (didit): nuestra página muestra las instrucciones y el
  consentimiento, y redirige a la página del proveedor.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, get_settings
from app.dependencies import DbSession
from app.errors import AppError, LinkAlreadyUsed, LinkNotFound, ProviderError
from app.models import VerificationStatus
from app.schemas import ErrorResponse, VideoUploadResponse
from app.services import hosted_flow, sessions, storage
from app.services.hosted import is_hosted
from app.services.processing import process_uploaded_video
from app.services.validation import detect_video, read_upload_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/verify", tags=["Página de verificación (cliente)"])
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _link_error(request: Request, exc: AppError) -> HTMLResponse:
    logger.info("Acceso a link no utilizable (%s)", type(exc).__name__)
    return templates.TemplateResponse(
        request, "link_error.html", {"message": exc.detail}, status_code=exc.status_code,
    )


# Va antes de /{token} para que "complete" no se interprete como un token.
@router.get(
    "/complete",
    response_class=HTMLResponse,
    summary="Regreso desde el proveedor",
    description="El proveedor alojado redirige acá al cliente cuando termina la verificación.",
)
def verification_complete(
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
    verificationSessionId: str | None = None,  # noqa: N803 (nombre definido por Didit)
):
    if verificationSessionId:
        verification = sessions.get_by_external_session(db, verificationSessionId)
        if verification is not None:
            logger.info("El cliente volvió del proveedor (verificación %s)", verification.id)
            background_tasks.add_task(hosted_flow.sync_result, verification.id)
    # Al cliente no se le muestra el resultado: la decisión la recibe la empresa.
    return templates.TemplateResponse(request, "verification_done.html")


@router.get(
    "/{token}",
    response_class=HTMLResponse,
    summary="Página de verificación",
    description="Página web que abre el cliente con el link único.",
)
def verification_page(token: str, request: Request, db: DbSession):
    try:
        verification = sessions.get_usable_by_token(db, token)
    except AppError as exc:
        return _link_error(request, exc)

    settings = get_settings()
    if is_hosted(verification.provider):
        return templates.TemplateResponse(request, "hosted_start.html", {"token": token, "error": None})

    config = {
        "uploadUrl": str(request.url_for("upload_video", token=token).path),
        "recordingSeconds": settings.recording_seconds,
        "maxVideoBytes": settings.max_video_bytes,
    }
    return templates.TemplateResponse(
        request, "verify.html", {"config": config, "recording_seconds": settings.recording_seconds},
    )


@router.post(
    "/{token}/start",
    response_class=HTMLResponse,
    summary="Iniciar la verificación en el proveedor",
    description="Solo para motores alojados. Crea la sesión en el proveedor y redirige al cliente.",
)
def start_hosted_verification(
    token: str,
    request: Request,
    db: DbSession,
    consent: Annotated[bool, Form()] = False,
):
    try:
        verification = sessions.get_usable_by_token(db, token)
    except AppError as exc:
        return _link_error(request, exc)
    if not is_hosted(verification.provider):
        return _link_error(request, LinkNotFound())

    if not consent:
        return templates.TemplateResponse(
            request, "hosted_start.html",
            {"token": token, "error": "Para continuar tenés que aceptar el uso de tus datos."},
            status_code=422,
        )

    try:
        url = hosted_flow.start_session(db, verification)
    except ProviderError:
        return templates.TemplateResponse(
            request, "hosted_start.html",
            {"token": token, "error": "No pudimos iniciar la verificación. Intentá de nuevo en unos minutos."},
            status_code=502,
        )
    except AppError as exc:
        return _link_error(request, exc)

    logger.info("Redirigiendo al proveedor (verificación %s)", verification.id)
    return RedirectResponse(url, status_code=303)


@router.post(
    "/{token}/video",
    name="upload_video",
    response_model=VideoUploadResponse,
    summary="Subir el video grabado",
    description="Solo para motores locales. Recibe el video grabado en la página. El link queda consumido.",
    responses={
        404: {"model": ErrorResponse, "description": "El link no existe."},
        410: {"model": ErrorResponse, "description": "El link expiró o ya fue utilizado."},
        413: {"model": ErrorResponse, "description": "El video supera el tamaño máximo."},
        415: {"model": ErrorResponse, "description": "Formato de video no permitido."},
    },
)
async def upload_video(
    token: str,
    db: DbSession,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(description="Video grabado con la cámara (WebM o MP4)."),
) -> VideoUploadResponse:
    verification = sessions.get_usable_by_token(db, token)
    if is_hosted(verification.provider):
        raise LinkNotFound()
    logger.info("Recibiendo video de la verificación %s", verification.id)

    data = await read_upload_limited(video, get_settings().max_video_bytes, "El video")
    kind = detect_video(data)

    # Se guarda primero con nombre temporal y se confirma solo si este
    # request es el que consume el link.
    temp_path = storage.save_video_temp(verification.id, data)
    expected_path = f"{verification.id}/video.{kind.extension}"
    if not sessions.mark_video_received(db, verification, expected_path, kind.mime_type):
        storage.discard(temp_path)
        raise LinkAlreadyUsed()
    storage.finalize_video(temp_path, verification.id, kind.extension)

    background_tasks.add_task(process_uploaded_video, verification.id)
    return VideoUploadResponse(status=VerificationStatus.VIDEO_RECEIVED, message="Video enviado correctamente.")
