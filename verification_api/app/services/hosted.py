"""Motores de verificación alojados (por ejemplo, Didit).

A diferencia de los motores locales de `verification.py`, acá el cliente no
graba en nuestra página: lo mandamos a la página del proveedor, que hace la
captura, la prueba de vida y la comparación facial. Nosotros:

1. Creamos la sesión en el proveedor (`create_session`).
2. Redirigimos al cliente a la URL que devuelve.
3. Cuando el proveedor avisa (webhook o regreso del cliente), consultamos el
   resultado (`fetch_result`) y lo traducimos a nuestros estados.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.services.verification import FaceMatchResult, LivenessResult


class HostedStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class HostedSession:
    session_id: str
    url: str


@dataclass(frozen=True)
class MediaFile:
    kind: str  # "video" o "selfie"
    url: str


@dataclass(frozen=True)
class DocumentResult:
    """Resultado de la lectura del documento hecha en la sesión del proveedor."""

    provider_status: str  # Approved, Declined o In Review.
    warnings: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HostedResult:
    status: HostedStatus
    provider_status: str  # Estado tal cual lo informa el proveedor.
    liveness: LivenessResult
    face_match: FaceMatchResult
    reason: str
    media: list[MediaFile] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    # Sólo si el workflow lee el documento en la sesión (modo "kyc") y ya hay resultado.
    document: DocumentResult | None = None


class HostedVerificationProvider(Protocol):
    name: str

    def create_session(
        self, *, verification_id: str, ci_photo_path: Path | None, ci_number: str, callback_url: str,
    ) -> HostedSession:
        """Crea la sesión en el proveedor y devuelve la URL para el cliente.

        `ci_photo_path` es None si el agente no cargó la foto (sólo en modo "kyc").
        """
        ...

    def fetch_result(self, session_id: str) -> HostedResult:
        """Consulta el estado y el resultado de la sesión."""
        ...

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> str:
        """Valida la firma del webhook y devuelve el ID de sesión del proveedor."""
        ...

    def download(self, url: str) -> tuple[bytes, str | None]:
        """Descarga un archivo del proveedor. Devuelve (contenido, content-type)."""
        ...


def _registry() -> dict[str, type[HostedVerificationProvider]]:
    # Import diferido para evitar imports circulares.
    from app.services.didit import DiditProvider

    return {"didit": DiditProvider}


def is_hosted(name: str) -> bool:
    return name in _registry()


def get_hosted_provider(name: str) -> HostedVerificationProvider:
    try:
        return _registry()[name]()
    except KeyError:
        raise ValueError(f"Motor alojado desconocido: {name!r}") from None
