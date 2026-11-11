"""Formulario del vendedor (simulado).

Simula la pantalla del sistema de créditos donde el vendedor carga la foto
y el número de CI del cliente. Envía los datos al servidor simulado de la
empresa, no directamente a la API de verificación.

Levantar con:
    streamlit run app.py
(desde la carpeta seller_app/, para que tome .streamlit/config.toml)
"""

import io
import logging
import re
from datetime import datetime
from pathlib import Path

import httpx
import qrcode
import streamlit as st
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    company_api_url: str = "http://localhost:8001"
    http_timeout_seconds: float = 30.0


settings = Settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logger = logging.getLogger("seller_app")

# Textos en español para cada estado de la verificación.
STATUS_LABELS = {
    "pending": "Pendiente: el cliente todavía no empezó la verificación.",
    "in_progress": "En curso: el cliente está haciendo la verificación.",
    "video_received": "Video recibido: resultado pendiente.",
    "processing": "Procesando el video.",
    "in_review": "En revisión manual en el proveedor.",
    "approved": "Aprobada.",
    "rejected": "Rechazada.",
    "expired": "El link expiró sin que el cliente complete la verificación.",
}


def normalize_ci(value: str) -> str:
    """Permite que el vendedor escriba la CI con puntos o espacios (4.567.890)."""
    return re.sub(r"[.\s-]", "", value)


def make_qr_png(url: str) -> bytes:
    image = qrcode.make(url, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return response.text
    # Los errores de validación de FastAPI vienen como lista.
    if isinstance(detail, list):
        return "; ".join(item.get("msg", str(item)) for item in detail)
    return str(detail)


def format_datetime(value: str) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M")


def submit_request(photo, back_photo, ci_number: str) -> None:
    files = {}
    if photo is not None:
        files["ci_photo"] = (photo.name, photo.getvalue(), photo.type)
    if back_photo is not None:
        files["ci_back_photo"] = (back_photo.name, back_photo.getvalue(), back_photo.type)
    logger.info("Enviando solicitud para la CI %s", ci_number)
    try:
        response = httpx.post(
            f"{settings.company_api_url}/credit-requests",
            files=files, data={"ci_number": ci_number}, timeout=settings.http_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        logger.error("Error de conexión: %s", exc)
        st.session_state.error = "No se pudo conectar con el sistema de la empresa."
        return
    if not response.is_success:
        st.session_state.error = f"No se pudo crear la verificación: {error_detail(response)}"
        return
    st.session_state.result = response.json()
    st.session_state.status = None


def fetch_status(request_id: str) -> None:
    try:
        response = httpx.get(
            f"{settings.company_api_url}/credit-requests/{request_id}/verification",
            timeout=settings.http_timeout_seconds,
        )
    except httpx.HTTPError:
        st.session_state.error = "No se pudo conectar con el sistema de la empresa."
        return
    if not response.is_success:
        st.session_state.error = f"No se pudo consultar el estado: {error_detail(response)}"
        return
    st.session_state.status = response.json()


def show_document(document: dict | None) -> None:
    """Resultado de la verificación del documento hecha por Didit.

    Según el modo de la API, es la verificación server-to-server de las fotos que
    cargó el vendedor (no cambia el estado: el vendedor lo evalúa por su cuenta) o
    la cédula que escaneó el cliente en la sesión de Didit.
    """
    if not document:
        return
    state = document["status"]
    if state == "valid":
        st.success("Documento validado.")
    elif state == "suspicious":
        st.error("Documento sospechoso. Revisá la cédula física antes de continuar.")
    elif state == "pending":
        st.info("Documento todavía sin verificar.")
    else:
        st.warning("No se pudo verificar el documento. Consultá de nuevo en unos minutos.")
    for warning in document.get("warnings") or []:
        st.markdown(f"- {warning}")
    data = document.get("data") or {}
    if state in ("valid", "suspicious") and data:
        read = [
            ("Número leído", data.get("document_number")),
            ("Nombre", data.get("full_name")),
            ("Vencimiento", data.get("expiration_date")),
        ]
        st.caption(" · ".join(f"{label}: {value}" for label, value in read if value))


def reset() -> None:
    for key in ("result", "status", "error"):
        st.session_state.pop(key, None)


# ---------- Interfaz ----------

st.set_page_config(page_title="Solicitud de crédito", layout="centered")
st.session_state.setdefault("result", None)
st.session_state.setdefault("status", None)
st.session_state.setdefault("error", None)

st.title("Solicitud de crédito")
st.caption("Verificación de identidad del cliente")

if st.session_state.error:
    st.error(st.session_state.error)
    st.session_state.error = None

result = st.session_state.result

if result is None:
    with st.form("credit_request"):
        photo = st.file_uploader("Foto del frente de la CI (opcional)", type=["jpg", "jpeg", "png"])
        back_photo = st.file_uploader("Foto del dorso de la CI (opcional)", type=["jpg", "jpeg", "png"])
        ci_raw = st.text_input("Número de CI", placeholder="Por ejemplo: 4567890")
        submitted = st.form_submit_button("Generar link de verificación", type="primary", width="stretch")

    if submitted:
        ci_number = normalize_ci(ci_raw)
        if not re.fullmatch(r"\d{5,10}", ci_number):
            st.error("El número de CI tiene que tener entre 5 y 10 dígitos.")
        else:
            with st.spinner("Generando link..."):
                submit_request(photo, back_photo, ci_number)
            st.rerun()
else:
    st.subheader("Link generado")
    st.write("Pedile al cliente que escanee el código QR o enviale el link.")

    col_info, col_qr = st.columns([3, 2], gap="large")
    with col_info:
        st.markdown(f"**ID de solicitud**  \n`{result['request_id']}`")
        st.markdown(f"**ID de verificación**  \n`{result['verification_id']}`")
        st.markdown(f"**Vence**  \n{format_datetime(result['expires_at'])}")
    with col_qr:
        st.image(make_qr_png(result["verification_url"]), width="stretch")

    st.markdown("**Link de verificación**")
    st.code(result["verification_url"], language=None)

    st.divider()
    col_status, col_new = st.columns(2)
    with col_status:
        if st.button("Consultar estado", type="primary", width="stretch"):
            fetch_status(result["request_id"])
            st.rerun()
    with col_new:
        st.button("Nueva solicitud", on_click=reset, width="stretch")

    status = st.session_state.status
    if status:
        label = STATUS_LABELS.get(status["status"], status["status"])
        if status["status"] == "approved":
            st.success(label)
        elif status["status"] in ("rejected", "expired"):
            st.error(label)
        else:
            st.info(label)
        show_document(status.get("document"))
        with st.expander("Detalle de la respuesta"):
            st.json(status)
