"""Conexión a la base de datos (SQLite con SQLAlchemy 2.0)."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
settings.database_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    # SQLite no permite por defecto usar la conexión desde otro hilo, y FastAPI
    # ejecuta las dependencias y tareas en segundo plano en un threadpool.
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Guarda fechas en UTC y las devuelve con zona horaria.

    SQLite no conserva la zona horaria, así que se guarda la fecha en UTC sin
    tzinfo y se le vuelve a agregar al leerla.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Las fechas deben tener zona horaria.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(UTC)


def init_db() -> None:
    # Importar los modelos registra las tablas en Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Agrega a las tablas existentes las columnas nuevas de los modelos.

    Alcanza para desarrollo con SQLite. En producción conviene usar
    migraciones de verdad (Alembic).
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
            if column.server_default is not None:
                ddl += f" DEFAULT '{column.server_default.arg}'"
            with engine.begin() as conn:
                conn.exec_driver_sql(ddl)
            logger.info("Columna agregada: %s.%s", table.name, column.name)


def get_db() -> Iterator[Session]:
    """Dependencia de FastAPI: una sesión de base de datos por request."""
    with SessionLocal() as db:
        yield db
