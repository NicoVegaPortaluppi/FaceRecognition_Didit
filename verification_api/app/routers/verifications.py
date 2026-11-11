"""Endpoints que consume el sistema de la empresa."""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Response, UploadFile, status

from app.config import get_settings
from app.dependencies import DbSession, require_api_key
from app.errors import CiPhotoRequired, RequestConflict
from app.models import VerificationStatus
from app.schemas import ErrorResponse, VerificationCreated, VerificationDetail
from app.services import document_check, hosted_flow, sessions
from app.services.hosted import is_hosted
from app.services.validation import detect_image, read_upload_limited

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/verifications",
    tags=["Verificaciones"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": ErrorResponse, "description": "API key inválida o ausente."}},
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=VerificationCreated,
    summary="Crear una verificación",
    description=(
        "Recibe la foto de la CI (frente y, opcionalmente, dorso), el número de CI y el ID de la "
        "solicitud de crédito. Crea una sesión de verificación y devuelve un link único y de un solo "
        "uso para el cliente.\n\n"
        "El resultado del documento aparece en `document` al consultar la verificación. Con "
        "DIDIT_WORKFLOW_MODE=biometric, las fotos de la CI se mandan a Didit en segundo plano para que "
        "verifique si el documento es auténtico. Con DIDIT_WORKFLOW_MODE=kyc, el cliente escanea su "
        "cédula en la sesión de Didit y las fotos que se cargan acá quedan sólo como registro.\n\n"
        "Si ya existe una sesión pendiente y vigente para el mismo `request_id`, se devuelve esa "
        "misma sesión con código 200 en lugar de crear otra."
    ),
    responses={
        200: {"model": VerificationCreated, "description": "Ya existía una sesión vigente para esa solicitud."},
        409: {"model": ErrorResponse, "description": "Existe una sesión vigente para la solicitud con otro número de CI."},
        413: {"model": ErrorResponse, "description": "La foto supera el tamaño máximo."},
        415: {"model": ErrorResponse, "description": "La foto no es JPG ni PNG."},
        422: {"description": "Datos inválidos (por ejemplo, número de CI con caracteres no numéricos, o "
              "falta la foto de la CI cuando es obligatoria)."},
    },
)
async def create_verification(
    response: Response,
    db: DbSession,
    background_tasks: BackgroundTasks,
    ci_number: Annotated[str, Form(
        pattern=r"^\d{5,10}$",
        description="Número de CI, solo dígitos (sin puntos ni espacios).",
        examples=["4567890"],
    )],
    request_id: Annotated[str, Form(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$",
        description="ID de la solicitud de crédito en el sistema de la empresa.",
        examples=["SOL-20261103-4F2A9C"],
    )],
    ci_photo: Annotated[UploadFile | None, File(
        description="Foto del frente de la cédula de identidad (JPG o PNG). Obligatoria, salvo con "
        "DIDIT_WORKFLOW_MODE=kyc: ahí el cliente escanea la cédula en la sesión de Didit.",
    )] = None,
    ci_back_photo: Annotated[UploadFile | None, File(
        description="Foto del dorso de la cédula (JPG o PNG). Opcional; mejora la verificación del documento.",
    )] = None,
) -> VerificationCreated:
    logger.info("Nueva solicitud de verificación (solicitud %s)", request_id)

    existing = sessions.find_active_by_request(db, request_id)
    if existing is not None:
        if existing.ci_number != ci_number:
            raise RequestConflict()
        logger.info("Se reutiliza la verificación vigente %s", existing.id)
        response.status_code = status.HTTP_200_OK
        return VerificationCreated.from_model(existing, sessions.build_verification_url(existing.token))

    settings = get_settings()
    # Modo "kyc": el cliente escanea la cédula en la sesión de Didit; las fotos son opcionales.
    document_in_session = settings.verification_provider == "didit" and settings.didit_workflow_mode == "kyc"
    max_bytes = settings.max_image_bytes
    photo, kind = None, None
    if ci_photo is not None and ci_photo.filename:
        photo = await read_upload_limited(ci_photo, max_bytes, "La foto de la CI")
        kind = detect_image(photo)
    elif not document_in_session:
        raise CiPhotoRequired()
    back_photo, back_kind = None, None
    if ci_back_photo is not None and ci_back_photo.filename:
        back_photo = await read_upload_limited(ci_back_photo, max_bytes, "La foto del dorso de la CI")
        back_kind = detect_image(back_photo)

    check_document = document_check.enabled()
    verification = sessions.create_verification(
        db,
        request_id=request_id,
        ci_number=ci_number,
        ci_photo=photo,
        ci_photo_extension=kind.extension if kind else None,
        ci_back_photo=back_photo,
        ci_back_photo_extension=back_kind.extension if back_kind else None,
        document_check=check_document or document_in_session,
    )
    if check_document:
        background_tasks.add_task(document_check.run, verification.id)
    return VerificationCreated.from_model(verification, sessions.build_verification_url(verification.token))


@router.get(
    "/{verification_id}",
    response_model=VerificationDetail,
    summary="Consultar el estado de una verificación",
    responses={404: {"model": ErrorResponse, "description": "No existe la verificación."}},
)
def get_verification(verification_id: str, db: DbSession, background_tasks: BackgroundTasks) -> VerificationDetail:
    verification = sessions.get_by_id(db, verification_id)

    # Con un motor alojado, si la sesión sigue abierta se consulta al proveedor.
    # Así el estado se actualiza aunque el webhook no haya llegado.
    if is_hosted(verification.provider) and verification.status in _SYNC_ON_READ:
        hosted_flow.sync_result(verification.id)
        db.refresh(verification)

    # Si quedaron archivos sin descargar, se reintenta en segundo plano.
    if verification.media_pending:
        background_tasks.add_task(hosted_flow.retry_pending_media, verification.id)

    # Si la verificación del documento falló, se reintenta en segundo plano.
    if verification.document_status == document_check.ERROR:
        background_tasks.add_task(document_check.run, verification.id)

    return VerificationDetail.from_model(verification)


_SYNC_ON_READ = {VerificationStatus.IN_PROGRESS, VerificationStatus.IN_REVIEW}
