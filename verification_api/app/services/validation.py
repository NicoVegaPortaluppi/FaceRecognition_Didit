"""Validación de archivos subidos.

El tipo de archivo se determina por su contenido (magic bytes) y no por el
nombre ni el Content-Type que manda el cliente, que se pueden falsificar.
"""

from dataclasses import dataclass

from fastapi import UploadFile

from app.errors import EmptyFile, FileTooLarge, UnsupportedFileType

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileKind:
    extension: str
    mime_type: str


async def read_upload_limited(upload: UploadFile, max_bytes: int, label: str) -> bytes:
    """Lee el archivo completo, cortando apenas supera `max_bytes`."""
    buffer = bytearray()
    while chunk := await upload.read(_CHUNK_SIZE):
        buffer += chunk
        if len(buffer) > max_bytes:
            max_mb = max_bytes / (1024 * 1024)
            raise FileTooLarge(f"{label} supera el tamaño máximo de {max_mb:g} MB.")
    if not buffer:
        raise EmptyFile(f"{label} está vacío.")
    return bytes(buffer)


def detect_image(data: bytes) -> FileKind:
    """Acepta solo JPG y PNG."""
    if data.startswith(b"\xff\xd8\xff"):
        return FileKind("jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind("png", "image/png")
    raise UnsupportedFileType("La foto de la CI tiene que ser una imagen JPG o PNG.")


def detect_video(data: bytes) -> FileKind:
    """Acepta los formatos que produce MediaRecorder en los navegadores móviles.

    - WebM (Chrome / Android, Firefox): empieza con la firma EBML 1A 45 DF A3.
    - MP4 (Safari / iOS, Chrome reciente): caja "ftyp" en los bytes 4 a 8.
    - QuickTime (algunas versiones de iOS): "ftyp" con marca "qt  ".
    """
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return FileKind("webm", "video/webm")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return FileKind("mov", "video/quicktime")
        return FileKind("mp4", "video/mp4")
    raise UnsupportedFileType("El video tiene que estar en formato WebM o MP4.")
