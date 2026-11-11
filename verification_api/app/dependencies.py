"""Dependencias compartidas por los routers."""

import secrets
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import InvalidApiKey

DbSession = Annotated[Session, Depends(get_db)]

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Clave entregada a la empresa para usar la API.",
)


def require_api_key(api_key: Annotated[str | None, Security(_api_key_header)]) -> None:
    # compare_digest evita filtrar información por diferencias de tiempo.
    if not api_key or not secrets.compare_digest(api_key, get_settings().api_key):
        raise InvalidApiKey()
