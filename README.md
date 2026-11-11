# Verificación de identidad con prueba de vida

Sistema para verificar la identidad de un cliente durante la solicitud de crédito:
el cliente graba un video corto con la cámara frontal de su celular y el servidor
verifica que es una persona real (liveness) y que su cara coincide con la foto de
su cédula (face match).

> **Etapa actual:** el flujo funciona de punta a punta, pero el análisis biométrico
> todavía no está implementado. El video se guarda localmente y el resultado queda
> en `pending`.

## Componentes

| Carpeta             | Qué es                                                    | Puerto |
|---------------------|-----------------------------------------------------------|--------|
| `verification_api/` | **API de verificación** (lo que se entrega a la empresa) + página del cliente | 8000 |
| `company_mock/`     | Servidor simulado del sistema de créditos de la empresa   | 8001   |
| `seller_app/`       | Formulario del vendedor (Streamlit)                       | 8501   |

```
Vendedor (Streamlit :8501)
   │  foto CI + número CI
   ▼
Empresa simulada (:8001) ── genera request_id ──► API de verificación (:8000)
   ▲                                                 │  verification_id + link único
   │◄──────────────── webhook firmado ───────────────┤
                                                     ▼
                              Cliente abre el link en el celular (HTTPS)
                              y graba el video ──► POST /verify/{token}/video
```

## Estructura de la API de verificación

```
verification_api/app/
├── main.py                 # App FastAPI, documentación, manejo de errores
├── config.py               # Configuración (pydantic-settings, lee .env)
├── db.py / models.py       # SQLite con SQLAlchemy
├── schemas.py              # Modelos Pydantic de request/response
├── errors.py               # Errores de la aplicación -> respuestas HTTP
├── dependencies.py         # API key, sesión de base de datos
├── routers/
│   ├── verifications.py    # POST /verifications, GET /verifications/{id}
│   ├── verify_page.py      # Páginas del cliente: /verify/{token}, /start, /video, /complete
│   └── provider_webhooks.py # POST /webhooks/didit
├── services/
│   ├── sessions.py         # Crear, buscar, expirar y consumir sesiones
│   ├── validation.py       # Validación de archivos por contenido (magic bytes)
│   ├── storage.py          # Archivos en storage/{verification_id}/
│   ├── webhook.py          # Notificaciones a la empresa, firmadas con HMAC-SHA256
│   ├── verification.py     # Motores locales: interfaz de liveness y face match (stub)
│   ├── processing.py       # Motor local: lo que pasa después de recibir el video
│   ├── hosted.py           # Motores alojados: interfaz común
│   ├── didit.py            # Motor alojado: integración con Didit
│   └── hosted_flow.py      # Motor alojado: iniciar sesión y sincronizar el resultado
├── templates/              # HTML de la página del cliente
└── static/                 # styles.css (variables de estilo en :root) y verify.js
```

## Instalación

Requiere Python 3.11 o superior. El proyecto usa un entorno de pyenv
(`ReimpexFaceID_Didit`) que se activa solo al entrar a la carpeta, por el archivo
`.python-version`.

```bash
pyenv virtualenv 3.14.3 ReimpexFaceID_Didit   # solo la primera vez
pyenv local ReimpexFaceID_Didit
pip install -r requirements.txt

cp verification_api/.env.example verification_api/.env
cp company_mock/.env.example     company_mock/.env
cp seller_app/.env.example       seller_app/.env
```

`API_KEY` y `WEBHOOK_SECRET` de `verification_api/.env` tienen que coincidir con
`VERIFICATION_API_KEY` y `WEBHOOK_SECRET` de `company_mock/.env`.

## Levantar los componentes

Cada uno en su propia terminal (los comandos completos están en `COMANDOS.txt`):

```bash
# 1. API de verificación
cd verification_api && uvicorn app.main:app --port 8000 --reload

# 2. Servidor simulado de la empresa
cd company_mock && uvicorn app.main:app --port 8001 --reload

# 3. Formulario del vendedor
cd seller_app && streamlit run app.py
```

- Documentación de la API: http://localhost:8000/docs
- Documentación del servidor simulado: http://localhost:8001/docs
- Formulario: http://localhost:8501

## Probar desde el celular (HTTPS)

Los navegadores solo dejan usar la cámara en HTTPS (o en `localhost`). La forma
más simple es un túnel de Cloudflare, que no necesita cuenta:

```bash
# Arch Linux
sudo pacman -S cloudflared

cloudflared tunnel --url http://localhost:8000
```

`cloudflared` muestra una URL del tipo `https://algo-al-azar.trycloudflare.com`.

1. Poné esa URL en `verification_api/.env`:
   ```
   PUBLIC_BASE_URL=https://algo-al-azar.trycloudflare.com
   ```
2. Reiniciá la API de verificación (con `--reload` no alcanza, porque no detecta
   los cambios en `.env`).

Solo hace falta exponer la API (puerto 8000): la página y la subida del video
están en el mismo servidor. El webhook y el formulario siguen funcionando por
`localhost`.

> La URL del quick tunnel cambia cada vez que se reinicia `cloudflared`. Para tener
> una URL fija hace falta un túnel con nombre (requiere cuenta de Cloudflare) o ngrok
> con un dominio reservado: `ngrok http 8000`.

## Probar el flujo completo

1. Abrí el formulario (http://localhost:8501), cargá las fotos del frente y del dorso de
   la CI (JPG o PNG) y el número de CI, y tocá **Generar link de verificación**.
2. Escaneá el código QR con el celular. Se abre la página de verificación.
3. Tocá **Comenzar**, dale permiso a la cámara, ubicá la cara en el óvalo y tocá **Grabar**.
4. Al terminar la grabación, el video se sube solo y aparece **Video enviado correctamente**.
5. Comprobá el resultado:
   - En la terminal del servidor simulado aparece el webhook `verification.video_received`.
   - En el formulario, **Consultar estado** muestra `Video recibido: resultado pendiente`.
   - Los archivos quedan en `verification_api/storage/{verification_id}/` (`ci.jpg` y `video.webm` o `video.mp4`).
   - Los webhooks recibidos se pueden ver en http://localhost:8001/webhooks/events.
6. Volvé a abrir el mismo link: tiene que decir que ya fue utilizado.

También se puede probar sin el celular, con `curl`:

```bash
curl -H "X-API-Key: cambiar-esta-api-key" \
     -F ci_photo=@ci.jpg -F ci_number=4567890 -F request_id=SOL-PRUEBA-1 \
     http://localhost:8000/verifications
```

## Comportamiento de la API

**Estados:** `pending` → `video_received` → `processing` → `approved` / `rejected`.
Si el link vence sin que se suba el video, la verificación pasa a `expired`.

**Link:**
- El token es aleatorio (`secrets.token_urlsafe(32)`) y distinto del ID de verificación.
- Expira a los `LINK_EXPIRATION_MINUTES` minutos.
- Se puede abrir varias veces (por ejemplo, si el cliente recarga la página), pero
  queda inutilizado cuando se sube el video. Si llegan dos subidas al mismo tiempo,
  solo una se acepta.

**Solicitud repetida:** si llega otro `POST /verifications` con el mismo `request_id`
mientras hay una sesión pendiente y vigente, se devuelve la misma sesión (código 200).
Si el número de CI es distinto, responde 409.

**Validaciones:**
- Número de CI: solo dígitos, de 5 a 10 caracteres.
- Foto de CI: JPG o PNG, hasta `MAX_IMAGE_MB`.
- Video: WebM, MP4 o QuickTime, hasta `MAX_VIDEO_MB`.
- El formato se detecta por el contenido del archivo, no por su nombre.

**Webhook:** `POST` JSON a `WEBHOOK_URL` con los eventos `verification.video_received`
y `verification.completed` (este último, cuando haya una decisión final). Se firma con
HMAC-SHA256 sobre `"{timestamp}.{body}"`; los headers y un ejemplo de validación
están en la descripción de `/docs` y en `company_mock/app/main.py`. Se reintenta
hasta `WEBHOOK_MAX_ATTEMPTS` veces.

## Motores de verificación

El motor se elige con `VERIFICATION_PROVIDER` en `verification_api/.env`. Cada
verificación guarda el motor con el que se creó, así que cambiarlo no afecta a las
sesiones que ya estaban en curso.

| Motor | Tipo | Qué ve el cliente |
|---|---|---|
| `stub` | Local | Nuestra página de grabación. El análisis devuelve `pending`. |
| `didit` | Alojado | Nuestra página de inicio y consentimiento, y después la página de Didit. |

### Motor local (`stub`, más adelante `deepface`)

El cliente graba el video en nuestra página y lo analizamos nosotros. Todo el
análisis está aislado en `verification_api/app/services/verification.py`:

1. Crear una clase que implemente `check_liveness(video_path)` y
   `face_match(video_path, ci_photo_path)`. Por ejemplo, `DeepFaceProvider`, con
   FasNet para anti-spoofing, ArcFace para la comparación y MediaPipe para detectar
   la cara en los cuadros.
2. Registrarla en `_PROVIDERS`.
3. Cambiar `VERIFICATION_PROVIDER` en el `.env`.

La decisión final de aprobar o rechazar se toma en `decide()`, siempre en el
servidor. Los endpoints no hay que tocarlos.

### Motor alojado: Didit

Didit hace la captura, la prueba de vida activa y la comparación con la foto de la CI.

**Flujo:**
1. La empresa llama a `POST /verifications`, igual que con el motor local.
2. El cliente abre nuestro link, lee las instrucciones y acepta el consentimiento.
3. `POST /verify/{token}/start` crea la sesión en Didit (`POST /v3/session/`) con la
   foto de la CI como `portrait_image`. Si pesa más de 2 MB, se achica antes. Después
   redirige al cliente a la página de Didit. Estado: `in_progress`.
4. Si el cliente cierra la pestaña y vuelve a abrir el link, va a la misma sesión de Didit.
5. Al terminar, Didit devuelve al cliente a `/verify/complete` y avisa por webhook
   a `/webhooks/didit`.
6. En los dos casos, la API consulta la decisión (`GET /v3/session/{id}/decision/`),
   guarda los puntajes (de 0 a 1), descarga el video y la selfie en
   `storage/{verification_id}/` y le manda a la empresa `verification.completed`.
   Aunque lleguen varios avisos, la empresa recibe uno solo.
7. Si el webhook no llega (por ejemplo, sin URL pública), `GET /verifications/{id}`
   consulta a Didit mientras la sesión esté abierta.

**Estados de Didit:** `Approved` → `approved`, `Declined` → `rejected`,
`In Review` → `in_review`, `Abandoned` / `Expired` → `expired`.

**Descarga del video y la selfie:**
- Se descargan solos al cerrarse la verificación, después de avisarle a la empresa.
- Cada archivo se intenta hasta `DIDIT_MEDIA_MAX_ATTEMPTS` veces (por defecto 3), con
  esperas de 2 s y 4 s. Desde el segundo intento se le piden a Didit links nuevos,
  porque los anteriores vencen.
- Si aun así falla, la verificación queda con `media_pending` y se reintenta al
  arrancar la API y cada vez que la empresa consulta `GET /verifications/{id}`.

**Configuración en la consola de Didit (business.didit.me):**
1. API key de la aplicación: `DIDIT_API_KEY`.
2. Workflow con prueba de vida y face match: `DIDIT_WORKFLOW_ID`.
3. Si la aplicación restringe dominios, agregar el de `PUBLIC_BASE_URL`, porque el
   regreso del cliente va a `PUBLIC_BASE_URL/verify/complete`.

**Modos del workflow (`DIDIT_WORKFLOW_MODE`).** Tiene que coincidir con lo que hace el
workflow de `DIDIT_WORKFLOW_ID`:

- `kyc` (por ejemplo, el workflow "Free KYC"): el cliente escanea su cédula en la sesión
  de Didit. Didit la lee y verifica, hace la prueba de vida y compara la cara con la
  cédula escaneada. No se manda `portrait_image` (Didit lo ignora en estos workflows) y
  se manda el número de CI que cargó el vendedor como
  `expected_details.identification_number`: si no coincide con el de la cédula, Didit
  lo marca y manda la sesión a revisión. El resultado del documento aparece en
  `document` y **sí** forma parte de la decisión final. La verificación server-to-server
  no se hace, aunque `DIDIT_DOCUMENT_CHECK` esté activo. Las fotos de la CI (frente y
  dorso) son opcionales: si el vendedor las carga, quedan sólo como registro. El número
  de CI sigue siendo obligatorio, porque es el que Didit compara con la cédula.
  `DIDIT_EXPECTED_ID_COUNTRY` (por ejemplo `PRY`) controla además el país de la cédula;
  en sandbox conviene dejarlo vacío porque Didit devuelve documentos de USA.
- `biometric` (por ejemplo, "Liveness activa + Face match CI"): sólo prueba de vida y
  face match contra la foto de la CI del vendedor, que se manda como `portrait_image`.
  El documento se verifica aparte, server-to-server (ver abajo).

**Verificación del documento (server-to-server, modo `biometric`).** Al crear la verificación, en
segundo plano, las fotos de la CI que cargó el vendedor (frente y dorso) se mandan a la
API de ID Verification de Didit (`POST /v3/id-verification/`, con
`perform_document_liveness`). Didit evalúa con su propio sistema si el documento es
auténtico (que no sea una pantalla o una fotocopia, foto reemplazada, datos y formato).
El cliente no tiene que volver a subir la cédula.

- El resultado **no cambia el estado** de la verificación (que depende de la prueba de
  vida y el face match). Se informa aparte en `document`: `valid` (Didit lo aprobó),
  `suspicious` (lo rechazó o lo mandó a revisión), `pending` o `error`, con los avisos
  de Didit y los datos leídos (número, nombre, vencimiento).
- En el formulario, **Consultar estado** muestra "Documento validado" en verde o
  "Documento sospechoso" en rojo, para que el vendedor lo evalúe.
- Se reintenta si Didit no responde, al arrancar la API y al consultar el estado.
- Se desactiva con `DIDIT_DOCUMENT_CHECK=false`.
- **En sandbox, Didit no analiza la imagen:** siempre aprueba y devuelve datos ficticios
  ("Sandbox User", documento `SANDBOX-0001`). Cómo responde con una cédula paraguaya real
  solo se ve con la key de producción (cada verificación de documento tiene costo).

**Webhook de Didit:** no hace falta configurarlo a mano. Con `PUBLIC_BASE_URL` apuntando
a la URL pública (HTTPS), corré desde `verification_api/`:

```bash
python -m scripts.setup_didit_webhook
```

El script crea (o actualiza, si ya existe) el destino del webhook en Didit, apuntando a
`{PUBLIC_BASE_URL}/webhooks/didit` con el evento `status.updated`, y guarda el secreto en
`DIDIT_WEBHOOK_SECRET` sin mostrarlo. Después hay que reiniciar la API. Didit no acepta
`localhost`, y **con el quick tunnel de cloudflared la URL cambia en cada reinicio**: cada
vez que cambie, hay que actualizar `PUBLIC_BASE_URL`, volver a correr el script y reiniciar
la API.

El texto de consentimiento de `templates/hosted_start.html` es provisorio: hay que
reemplazarlo por el texto legal de la empresa.

Para cambiar el estilo visual:
- Página del cliente: variables en `:root` de `verification_api/app/static/styles.css`.
- Formulario: `seller_app/.streamlit/config.toml`.
