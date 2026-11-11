"""Almacenamiento local de archivos.

Estructura:
    storage/{verification_id}/ci.jpg
    storage/{verification_id}/video.webm

En la base se guardan rutas relativas a STORAGE_DIR, así se puede mover la
carpeta (o cambiar a otro almacenamiento) sin reescribir los registros.
"""

import logging
import secrets
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _verification_dir(verification_id: str) -> Path:
    directory = get_settings().storage_dir / verification_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def absolute_path(relative_path: str) -> Path:
    return get_settings().storage_dir / relative_path


def save_ci_photo(verification_id: str, data: bytes, extension: str) -> str:
    path = _verification_dir(verification_id) / f"ci.{extension}"
    path.write_bytes(data)
    logger.info("Foto de CI guardada: %s (%d bytes)", path, len(data))
    return f"{verification_id}/{path.name}"


def save_video_temp(verification_id: str, data: bytes) -> Path:
    """Guarda el video con un nombre temporal hasta confirmar la subida."""
    path = _verification_dir(verification_id) / f"video.{secrets.token_hex(4)}.part"
    path.write_bytes(data)
    return path


def finalize_video(temp_path: Path, verification_id: str, extension: str) -> str:
    final_path = temp_path.with_name(f"video.{extension}")
    temp_path.replace(final_path)
    logger.info("Video guardado: %s (%d bytes)", final_path, final_path.stat().st_size)
    return f"{verification_id}/{final_path.name}"


def save_file(verification_id: str, name: str, data: bytes) -> str:
    """Guarda un archivo con nombre fijo (por ejemplo, la selfie del proveedor)."""
    path = _verification_dir(verification_id) / name
    path.write_bytes(data)
    logger.info("Archivo guardado: %s (%d bytes)", path, len(data))
    return f"{verification_id}/{name}"


def discard(path: Path) -> None:
    path.unlink(missing_ok=True)
