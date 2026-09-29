# Action Colleague — API

API de la plataforma de creación de cursos (con IA o con material propio). FastAPI + SQLAlchemy sobre Postgres (Supabase), desplegada en Railway.

## Estructura

```
app/
  main.py          create_app(): middlewares (proxy, CORS, logs), routers, health
  core/            config (variables de entorno), logging JSON, seguridad (JWT + bcrypt)
  api/deps.py      sesión de BD y usuario autenticado
  api/routes/      endpoints bajo /api/v1
  db/              modelos, sesión y migraciones (migrate.py)
  services/        integraciones (almacenamiento, IA, diapositivas, video)
  worker/          cola de trabajos en Postgres y sus handlers (python -m app.worker)
alembic/           migraciones del esquema (Alembic)
scripts/           utilidades de desarrollo (seed local)
tests/             pytest
```

## Desarrollo local

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
cp .env.example .env              # por defecto usa SQLite local
SEED_PASSWORD='elige-una-clave' .venv/bin/python -m scripts.seed_dev
.venv/bin/uvicorn app.main:app --reload --port 8001
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

## API (`/api/v1`)

| Área | Rutas | Quién |
|---|---|---|
| Sesión | `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/me` (GET y PATCH) | todos |
| Biblioteca y editor | `/courses`, `/courses/{id}` (+ `publish`, `unpublish`, `archive`, `preview`), `/courses/{id}/modules`, `/modules/{id}`, `/modules/{id}/evaluation` | admin |
| Participantes y resultados | `/courses/{id}/participants`, `/courses/{id}/participants/{user_id}/attempts`, `/courses/{id}/analytics` | admin |
| Equipo y panel | `/users`, `/users/{id}/courses`, `/dashboard` | admin |
| Aprender | `/learn/courses`, `/learn/courses/{id}`, `/learn/modules/{id}/quiz`, `/quiz/attempts`, `/complete`, `/position` | colaborador inscrito |
| Medios y trabajos | `/media/uploads`, `/media/{id}/complete`, `/media/{id}`, `/courses/{id}/cover`, `/courses/{id}/materials`, `/jobs` | admin |
| Estudio IA y video | `/courses/{id}/outline` (+ `generate`), `/courses/{id}/draft`, `/modules/{id}/storyboard`, `/slides/preview`, `/studio/*`, `/courses/{id}/render`, `/modules/{id}/render`, `/modules/{id}/recording`, `/modules/{id}/transcribe` | admin |

Reglas clave:
- Los módulos se desbloquean en orden. Un módulo con evaluación se completa aprobándola y uno sin evaluación, al marcarlo como visto.
- Si el admin edita una evaluación mientras alguien la responde, ese intento no se califica ni se cuenta (409) y el quiz se recarga.
- Cambiar la contraseña propia cierra todas las sesiones (el frontend vuelve a iniciar sesión con la nueva).
- El colaborador nunca recibe las respuestas correctas. Las opciones llevan IDs opacos (HMAC) y la calificación ocurre en el servidor (`app/services/quiz.py`, la única implementación). La solución se muestra solo al aprobar: mostrada al agotar los intentos, bastaría con que el admin diera más intentos para aprobar sin saber. Antes de aprobar, cada intento dice qué preguntas estuvieron bien.

## Medios y trabajos en segundo plano

- **Subidas directas**: el navegador pide `POST /media/uploads` y sube el archivo directo a Storage (PUT firmado hasta 6 MB, TUS reanudable por encima), sin pasar por el API. Luego `POST /media/{id}/complete` lo manda a procesar. La firma dura 2 h: una subida más larga pide otra con `POST /media/{id}/upload-target`. Un archivo ya recibido no se puede reemplazar con la misma firma.
- **Procesamiento** (`media.process`): video a MP4 H.264/AAC 8 bits de máximo 1080p y 30 fps, sin deformar (4:3 y vertical se mantienen), HDR del celular convertido a SDR, y con portada; al reemplazar un video, documento o portada se borra el anterior; texto de documentos (PDF, DOCX, PPTX, TXT); una imagen por página de presentaciones PDF; imágenes a JPEG.
- **Cola** en Postgres (`jobs`) con *leases*: un trabajo solo se reintenta si su proceso dejó de renovarlo (deploys solapados seguros); los errores reintentan con espera creciente y un proceso caído cuenta como intento. Por defecto corre dentro del API; para separarlo: `WORKER_ENABLED=false` en el API y un servicio con `python -m app.worker` (sin healthcheck HTTP). Al apagarse (deploy), el proceso devuelve sus trabajos en curso a la cola sin gastarles un intento, y cada versión solo toma los tipos de trabajo que conoce.
- Los medios nuevos se sirven con **URLs firmadas** de corta duración (bucket privado). Supabase limita el tamaño por archivo: súbelo en *Storage → Settings* (p. ej. 2 GB) para videos largos.

## Estudio IA

1. `POST /courses/{id}/outline/generate`: con el brief y los documentos subidos al curso, la IA propone título, objetivos y módulos (trabajo `ai.outline`).
2. `PUT /courses/{id}/outline`: el admin aprueba (y edita) la propuesta y se crean los módulos.
3. `POST /courses/{id}/draft`: por módulo, la IA escribe el guion en escenas (diapositiva + narración), un resumen de lectura y la evaluación (`ai.module_draft`). El guion se puede editar o regenerar con indicaciones.
4. `POST /modules/{id}/evaluation/generate`: sugiere preguntas desde el contenido de cualquier módulo (también manual); se revisan antes de guardar.

Las diapositivas se dibujan en el servidor a 1920×1080 (`app/services/slides`): siete layouts de marca, tipografías libres incluidas (OFL), texto que se ajusta solo y zona reservada para el presentador. `POST /slides/preview` devuelve exactamente la misma imagen que usará el video.

## Video IA (híbrido)

`POST /courses/{id}/render` (o `/modules/{id}/render`) produce cada módulo con guion (trabajo `video.render`):

1. **Narración**: ElevenLabs por escena con tiempos por carácter (la voz elegida o clonada). Las duraciones salen del audio real, unido a nivel de muestra con pausas, y normalizado (`loudnorm`); de los tiempos salen los subtítulos WebVTT.
2. **Presentador**: HeyGen v3 anima el avatar con **esa misma narración**, en cuadrado 720p. El trabajo se reprograma mientras HeyGen trabaja (no ocupa un worker) y, si HeyGen falla o no está configurado, el video sale igual sin presentador y con un aviso.
3. **Composición** (FFmpeg): diapositivas de marca a 1920×1080 con transiciones, burbuja circular con aro naranja (recorte cuadrado, nunca estirado), H.264/AAC con `+faststart`, portada y subtítulos. Todo va a Storage privado y reemplaza el video anterior del módulo.

## Grabación y subtítulos

- `POST /modules/{id}/recording` (`video.compose_recording`): une la grabación de cámara del admin con su presentación en PDF. Las diapositivas van a pantalla completa y cambian en el segundo exacto en que se cambiaron al grabar (`timeline: [{at, slide}]`), con la cámara en la burbuja del presentador, sin deformar. Sin presentación, la grabación procesada es el video del módulo. El trabajo espera a que ambas subidas terminen de procesarse.
- **Subtítulos automáticos** (`media.transcribe`): cada video nuevo de un módulo (subido, grabado o migrado) se transcribe con ElevenLabs Scribe; salen subtítulos WebVTT y una transcripción que la IA usa para sugerir la evaluación del módulo. `POST /modules/{id}/transcribe` lo repite a mano. Sin `ELEVENLABS_API_KEY` no se generan.

## Medios de la app anterior

Cada arranque encola `legacy.migrate` (idempotente, una sola copia activa):

- Convierte las evaluaciones antiguas (`questions_json`) al formato canónico (`spec`).
- Pone fecha de finalización a los cursos completados antes de que existiera (la del último módulo completado).
- Copia al bucket privado los videos que la app anterior dejó en buckets públicos del proyecto y los procesa como cualquier subida (portada, duración, subtítulos). El archivo original no se toca.
- Los videos que siguen en HeyGen esperan a que el copiado de abajo los pase a Storage; el trabajo lo vuelve a revisar cada 6 h hasta el apagado de la API de HeyGen.

## Migraciones

La app aplica las migraciones al arrancar (`app.db.migrate`), dentro de una transacción con un lock para que dos contenedores no migren a la vez. También se pueden correr a mano:

```bash
.venv/bin/python -m app.db.migrate
```

- `0001_baseline` describe el esquema que producción ya tenía (creado con `create_all`). Si la base tiene tablas pero no `alembic_version`, se marca (`stamp`) en esa revisión y se sigue desde ahí.
- Las migraciones son **solo aditivas**: la versión anterior de la app debe seguir funcionando con el esquema nuevo.
- La base de datos es compartida con otra aplicación (tablas `wendy_*`): Alembic solo gestiona lo que declaran los modelos y nunca propone borrar tablas o columnas ajenas.

Nueva migración (revisa siempre el archivo generado):

```bash
DATABASE_URL=postgresql://…local… .venv/bin/python -m app.db.migrate   # la base al día primero
DATABASE_URL=postgresql://…local… .venv/bin/alembic revision --autogenerate --rev-id 0003_slug -m "descripcion"
.venv/bin/alembic check   # en CI: falla si los modelos y las migraciones no coinciden
```

- Autogenera contra PostgreSQL (con SQLite salen diferencias falsas).
- La tabla de versiones se llama `action_colleague_alembic_version` (la base es compartida).
- Cada migración corre con `lock_timeout` (5 s) y se reintenta: nunca queda en cola detrás de la versión anterior, que sigue atendiendo durante el deploy.
- No uses `CREATE INDEX CONCURRENTLY` ni `autocommit_block` (confirman a mitad de camino y sueltan el lock), y no metas migraciones de datos pesadas: el arranque tiene 120 s para pasar el healthcheck.

Los tests de migraciones corren contra SQLite y contra un PostgreSQL 16 embebido (`pgserver`).

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `DATABASE_URL` | en producción | Postgres de Supabase (pooler). En local, SQLite. |
| `JWT_SECRET` (o `SECRET_KEY`) | en producción | Mínimo 32 caracteres. La app no arranca en producción sin ella. |
| `ACCESS_TOKEN_MINUTES` | no | Vida del access token en minutos (60 por defecto; el frontend lo renueva solo con el refresh token de 30 días). |
| `LOG_LEVEL` | no | `INFO` por defecto. |
| `CORS_ORIGINS` | no | Orígenes permitidos separados por coma. |
| `CORS_ORIGIN_REGEX` | no | Regex adicional de orígenes (p. ej. previews de Vercel). |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | en producción | Storage de Supabase (solo backend). Acepta las llaves nuevas `sb_secret_…`. |
| `MEDIA_BUCKET` | no | Bucket **privado** de los medios (por defecto `course-media`; se crea solo). |
| `STORAGE_BACKEND` | no | `auto` (Supabase si está configurado; si no, disco local en desarrollo), `supabase` o `local`. |
| `LOCAL_STORAGE_DIR`, `PUBLIC_API_URL` | no | Solo desarrollo: carpeta de los archivos y URL pública del API para sus enlaces. |
| `WORKER_ENABLED`, `WORKER_CONCURRENCY` | no | Trabajos en segundo plano dentro del API (por defecto sí, 2 a la vez). |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | para IA | Estudio IA (estructura, guiones y evaluaciones con salidas estructuradas). |
| `USE_FAKE_PROVIDERS` | no | Solo desarrollo y e2e: IA, voz y presentador falsos y deterministas, sin llaves ni red. |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | para voz | Narración (voz por defecto y modelo `eleven_multilingual_v2`) y subtítulos automáticos (Scribe). |
| `HEYGEN_API_KEY`, `HEYGEN_ENGINE` | para presentador | Presentador IA con la API v3 (`avatar_iii` por costo; `avatar_iv`/`avatar_v` más naturales). Sin llave, los videos salen sin presentador. |

El entorno se detecta con `RAILWAY_ENVIRONMENT_NAME` (Railway lo define) o `ENVIRONMENT`.

## Despliegue (Railway)

- Imagen: `Dockerfile` (usuario no root, FFmpeg incluido). Arranca con `uvicorn app.main:app`; `main.py` se mantiene como alias (`uvicorn main:app` o `python main.py`).
- `railway.json` define el healthcheck en `/health`: si la configuración es inválida (p. ej. falta `JWT_SECRET`), el nuevo deploy no recibe tráfico y el anterior sigue activo.

## Videos de HeyGen

HeyGen apaga su API v1/v2 el 31-oct-2026. Al arrancar (y cada 30 min durante un día) la app copia a Storage los videos que aún apuntan a HeyGen; luego `legacy.migrate` los mueve al bucket privado. Pasada manual:

```bash
.venv/bin/python -m app.services.heygen_persist
```

## Logs

Todos los logs salen en JSON por stdout (un objeto por línea) con `ts`, `level`, `logger`, `message` (el nombre del evento) y campos estructurados; cada request lleva `x-request-id`. No se usa `print()`.
