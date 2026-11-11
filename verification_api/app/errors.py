"""Errores de la aplicación.

Los servicios lanzan estas excepciones sin saber nada de HTTP; un handler
registrado en `main.py` las convierte en respuestas JSON `{"detail": "..."}`.
"""


class AppError(Exception):
    status_code: int = 400
    default_detail: str = "Error en la solicitud."

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class InvalidApiKey(AppError):
    status_code = 401
    default_detail = "API key inválida o ausente."


class VerificationNotFound(AppError):
    status_code = 404
    default_detail = "No existe una verificación con ese ID."


class RequestConflict(AppError):
    status_code = 409
    default_detail = "Ya existe una verificación activa para esa solicitud con otros datos."


class FileTooLarge(AppError):
    status_code = 413
    default_detail = "El archivo supera el tamaño máximo permitido."


class UnsupportedFileType(AppError):
    status_code = 415
    default_detail = "Formato de archivo no permitido."


class EmptyFile(AppError):
    status_code = 422
    default_detail = "El archivo está vacío."


class CiPhotoRequired(AppError):
    status_code = 422
    default_detail = "Falta la foto de la CI: con este motor de verificación es obligatoria."


class ProviderError(AppError):
    status_code = 502
    default_detail = "No se pudo comunicar con el proveedor de verificación."


class InvalidWebhookSignature(AppError):
    status_code = 401
    default_detail = "Firma del webhook inválida."


# --- Errores del link público ---

class LinkNotFound(AppError):
    status_code = 404
    default_detail = "El link no es válido. Revisá que esté completo o pedile uno nuevo al vendedor."


class LinkExpired(AppError):
    status_code = 410
    default_detail = "El link expiró. Pedile al vendedor que genere uno nuevo."


class LinkAlreadyUsed(AppError):
    status_code = 410
    default_detail = "Este link ya fue utilizado. Si necesitás verificarte de nuevo, pedile un link nuevo al vendedor."
