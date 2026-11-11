"""Punto de entrada de la API de verificación de identidad.

Levantar con:
    uvicorn app.main:app --port 8000 --reload
(desde la carpeta verification_api/)
"""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, get_settings
from app.db import init_db
from app.errors import AppError
from app.routers import provider_webhooks, verifications, verify_page
from app.services import document_check, hosted_flow
from app.services.hosted import get_hosted_provider, is_hosted
from app.services.verification import get_provider

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app")
# httpx registra cada URL completa, incluidos los links firmados de los videos
# de Didit. No deben quedar en los logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)

    # Falla al arrancar si el motor no existe o le falta configuración.
    if is_hosted(settings.verification_provider):
        get_hosted_provider(settings.verification_provider)
        mode = "alojado (el cliente se verifica en la página del proveedor)"
    else:
        get_provider(settings.verification_provider)
        mode = "local (el cliente graba en nuestra página)"

    logger.info("API lista. URL pública de los links: %s", settings.public_base_url)
    logger.info("Motor de verificación: %s, %s", settings.verification_provider, mode)

    # Reintenta en segundo plano las descargas que quedaron pendientes.
    threading.Thread(target=hosted_flow.retry_all_pending_media, daemon=True).start()
    # Reintenta las verificaciones de documento que quedaron sin terminar.
    threading.Thread(target=document_check.retry_unfinished, daemon=True).start()
    yield


DESCRIPTION = """
API para verificar la identidad de un cliente mediante una prueba de vida y la
comparación de su cara con la foto de su cédula de identidad (CI).

## Flujo

1. El sistema de la empresa llama a `POST /verifications` con la foto de la CI,
   el número de CI y el ID de la solicitud de crédito.
2. La API devuelve un **ID de verificación** y un **link único** que se le
   envía al cliente. El link expira y es de un solo uso.
3. El cliente abre el link en su celular y hace la prueba de vida con la cámara frontal.
4. La API notifica al sistema de la empresa mediante un **webhook** firmado.
5. La empresa puede consultar el estado en cualquier momento con `GET /verifications/{id}`.

## Estados

`pending` → `in_progress` (o `video_received` / `processing`) → `approved` | `rejected`.
También `in_review` (revisión manual en el proveedor) y `expired` (no se completó a tiempo).

## Autenticación

Los endpoints de `/verifications` requieren el header `X-API-Key`.

## Webhooks

Se envía un `POST` JSON a la URL configurada con los eventos
`verification.video_received` y `verification.completed`. Headers:

- `X-Webhook-Event`: tipo de evento.
- `X-Webhook-Timestamp`: segundos Unix.
- `X-Webhook-Signature`: `sha256=` + HMAC-SHA256 de `"{timestamp}.{body}"` con el secreto compartido.
"""

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Verificaciones", "description": "Endpoints para el sistema de la empresa."},
        {"name": "Página de verificación (cliente)", "description": "Páginas que abre el cliente con el link."},
        {"name": "Webhooks de proveedores", "description": "Avisos que envían los proveedores alojados (Didit)."},
        {"name": "Estado del servicio"},
    ],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.include_router(verifications.router)
app.include_router(verify_page.router)
app.include_router(provider_webhooks.router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info("%s %s -> %d: %s", request.method, request.url.path, exc.status_code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health", tags=["Estado del servicio"], summary="Chequeo de salud")
def health() -> dict[str, str]:
    return {"status": "ok"}
