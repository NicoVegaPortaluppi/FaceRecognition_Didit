"""Configuración de la API de verificación.

Todos los valores se leen desde variables de entorno o desde el archivo
`verification_api/.env` (ver `.env.example`). Las rutas relativas se resuelven
respecto de la carpeta `verification_api/`, así el servicio funciona igual sin
importar desde dónde se lo lance.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Carpeta raíz del servicio (verification_api/).
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    app_name: str = "API de Verificación de Identidad"
    log_level: str = "INFO"

    # URL pública con la que se arman los links que recibe el cliente.
    # Para probar desde el celular tiene que ser la URL HTTPS del túnel.
    public_base_url: str = "http://localhost:8000"

    # --- Seguridad ---
    # Clave que debe enviar el sistema de la empresa en el header X-API-Key.
    api_key: str = Field(default="dev-api-key", min_length=8)

    # --- Webhook hacia el sistema de la empresa ---
    webhook_url: str | None = "http://localhost:8001/webhooks/verification"
    webhook_secret: str = Field(default="dev-webhook-secret", min_length=8)
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = Field(default=3, ge=1)

    # --- Sesión de verificación ---
    link_expiration_minutes: int = Field(default=15, ge=1)
    recording_seconds: int = Field(default=5, ge=2, le=30)

    # --- Límites de archivos ---
    max_image_mb: float = Field(default=5, gt=0)
    max_video_mb: float = Field(default=30, gt=0)

    # --- Almacenamiento ---
    storage_dir: Path = Path("storage")
    database_path: Path = Path("data/verifications.db")

    # --- Motor de verificación (liveness + face match) ---
    # Motores locales (el cliente graba en nuestra página y analizamos el video):
    #   "stub" devuelve resultados pendientes. Más adelante: "deepface".
    # Motores alojados (el cliente hace la verificación en la página del proveedor):
    #   "didit".
    verification_provider: str = "stub"

    # --- Didit ---
    didit_api_key: str | None = None
    didit_workflow_id: str | None = None
    didit_webhook_secret: str | None = None
    didit_base_url: str = "https://verification.didit.me"
    didit_timeout_seconds: float = 20.0
    didit_language: str = "es"
    # Descargar el video y la selfie de la prueba de vida al recibir la decisión.
    # Los links de Didit vencen (4 horas por defecto).
    didit_download_media: bool = True
    # Intentos por archivo antes de dejarlo pendiente para un reintento posterior.
    didit_media_max_attempts: int = Field(default=3, ge=1)
    # Qué hace el workflow de Didit (tiene que coincidir con DIDIT_WORKFLOW_ID):
    #   "biometric": sólo prueba de vida y face match contra la foto de la CI que cargó
    #       el agente (se manda como portrait_image). El documento se verifica aparte,
    #       server-to-server, si DIDIT_DOCUMENT_CHECK está activo.
    #   "kyc": el cliente escanea su cédula en la sesión de Didit (lectura del documento,
    #       prueba de vida y face match contra la cédula escaneada). No se manda
    #       portrait_image ni se hace la verificación server-to-server; se manda el número
    #       de CI que cargó el agente para que Didit lo compare con el de la cédula.
    didit_workflow_mode: Literal["biometric", "kyc"] = "biometric"
    # Sólo en modo "kyc": país esperado de la cédula (ISO alfa-3, por ejemplo PRY). Si la
    # cédula es de otro país, Didit lo marca. Vacío: no se controla. En sandbox Didit
    # devuelve documentos de USA, así que conviene dejarlo vacío para probar.
    didit_expected_id_country: str | None = None
    # Verificación del documento (server-to-server, sólo en modo "biometric"): al crear
    # la verificación se le mandan a Didit las fotos de la CI (frente y dorso) para que
    # evalúe si es auténtica. Corre en segundo plano y no cambia el estado final: se
    # informa aparte.
    didit_document_check: bool = True
    didit_document_timeout_seconds: float = 60.0
    didit_document_max_attempts: int = Field(default=3, ge=1)

    @field_validator("storage_dir", "database_path")
    @classmethod
    def _resolve_relative(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def max_image_bytes(self) -> int:
        return int(self.max_image_mb * 1024 * 1024)

    @property
    def max_video_bytes(self) -> int:
        return int(self.max_video_mb * 1024 * 1024)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
