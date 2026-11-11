"""Webhooks que recibimos de los proveedores alojados."""

import logging

from fastapi import APIRouter, BackgroundTasks, Request

from app.dependencies import DbSession
from app.errors import ProviderError
from app.schemas import ErrorResponse
from app.services import hosted_flow, sessions
from app.services.hosted import get_hosted_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks de proveedores"])


@router.post(
    "/didit",
    summary="Webhook de Didit",
    description=(
        "Didit avisa acá cada cambio de estado de una sesión. Se valida la firma "
        "(`X-Signature` o `X-Signature-V2`) y después se consulta el resultado a la API "
        "de Didit, en lugar de confiar solo en el contenido del webhook."
    ),
    responses={401: {"model": ErrorResponse, "description": "Firma inválida."}},
)
async def didit_webhook(request: Request, db: DbSession, background_tasks: BackgroundTasks) -> dict[str, bool]:
    try:
        provider = get_hosted_provider("didit")
    except ValueError as exc:
        logger.error("Webhook de Didit recibido pero Didit no está configurado: %s", exc)
        raise ProviderError("Didit no está configurado.") from exc

    body = await request.body()
    session_id = provider.parse_webhook(body, request.headers)

    verification = sessions.get_by_external_session(db, session_id) if session_id else None
    if verification is None:
        # Se responde 200 igual para que Didit no reintente un evento que no es nuestro.
        logger.info("Webhook de Didit para una sesión desconocida (%s)", session_id)
        return {"received": True}

    background_tasks.add_task(hosted_flow.sync_result, verification.id)
    return {"received": True}
