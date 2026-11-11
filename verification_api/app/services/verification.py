"""Motor de verificación biométrica (liveness + face match).

Este módulo define la interfaz que usa el resto del sistema. Los endpoints no
conocen la implementación concreta: solo llaman a `get_provider()` y a
`decide()`. Para cambiar de motor (DeepFace + MediaPipe, Didit, etc.) alcanza
con agregar una clase que cumpla `VerificationProvider`, registrarla en
`_PROVIDERS` y cambiar VERIFICATION_PROVIDER en el `.env`.

La decisión de aprobar o rechazar se toma siempre acá, en el servidor.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"  # Sin resultado todavía (motor no implementado o análisis inconcluso).


@dataclass(frozen=True)
class LivenessResult:
    status: CheckStatus
    score: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FaceMatchResult:
    status: CheckStatus
    score: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Decision:
    # None significa que todavía no hay una decisión final.
    status: Literal["approved", "rejected"] | None
    reason: str


class VerificationProvider(Protocol):
    """Interfaz que tiene que cumplir cualquier motor de verificación."""

    name: str

    def check_liveness(self, video_path: Path) -> LivenessResult:
        """Determina si el video corresponde a una persona real frente a la cámara."""
        ...

    def face_match(self, video_path: Path, ci_photo_path: Path) -> FaceMatchResult:
        """Compara la cara del video con la foto de la cédula."""
        ...


class StubProvider:
    """Implementación provisoria: no analiza nada y devuelve `pending`."""

    name = "stub"

    def check_liveness(self, video_path: Path) -> LivenessResult:
        logger.info("[stub] check_liveness(%s): sin implementar", video_path.name)
        return LivenessResult(status=CheckStatus.PENDING, detail="Prueba de vida no implementada.")

    def face_match(self, video_path: Path, ci_photo_path: Path) -> FaceMatchResult:
        logger.info("[stub] face_match(%s, %s): sin implementar", video_path.name, ci_photo_path.name)
        return FaceMatchResult(status=CheckStatus.PENDING, detail="Comparación facial no implementada.")


# Registro de motores disponibles. Ejemplo a futuro:
#   "deepface": DeepFaceProvider,   # FasNet (anti-spoofing) + ArcFace + MediaPipe
#   "didit": DiditProvider,         # Proveedor externo
_PROVIDERS: dict[str, type[VerificationProvider]] = {
    "stub": StubProvider,
}


def get_provider(name: str) -> VerificationProvider:
    try:
        return _PROVIDERS[name]()
    except KeyError:
        raise ValueError(
            f"Motor de verificación desconocido: {name!r}. Opciones: {', '.join(_PROVIDERS)}"
        ) from None


def decide(liveness: LivenessResult, match: FaceMatchResult) -> Decision:
    """Combina los resultados en una decisión final."""
    if liveness.status is CheckStatus.FAILED:
        return Decision("rejected", liveness.detail or "No se superó la prueba de vida.")
    if match.status is CheckStatus.FAILED:
        return Decision("rejected", match.detail or "La cara no coincide con la foto de la CI.")
    if liveness.status is CheckStatus.PASSED and match.status is CheckStatus.PASSED:
        return Decision("approved", "Prueba de vida y comparación facial superadas.")
    return Decision(None, "El análisis biométrico todavía no está implementado.")
