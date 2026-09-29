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
  services/        integraciones (almacenamiento, IA, video)
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
| `ACCESS_TOKEN_MINUTES` | no | Vida del access token (por defecto 7 días, como antes). |
| `LOG_LEVEL` | no | `INFO` por defecto. |
| `CORS_ORIGINS` | no | Orígenes permitidos separados por coma. |
| `CORS_ORIGIN_REGEX` | no | Regex adicional de orígenes (p. ej. previews de Vercel). |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | en producción | Storage de Supabase (solo backend). Acepta las llaves nuevas `sb_secret_…`. |
| `MEDIA_BUCKET` | no | Bucket **privado** de los medios (por defecto `course-media`; se crea solo). |
| `STORAGE_BACKEND` | no | `auto` (Supabase si está configurado; si no, disco local en desarrollo), `supabase` o `local`. |
| `LOCAL_STORAGE_DIR`, `PUBLIC_API_URL` | no | Solo desarrollo: carpeta de los archivos y URL pública del API para sus enlaces. |
| `WORKER_ENABLED`, `WORKER_CONCURRENCY` | no | Trabajos en segundo plano dentro del API (por defecto sí, 2 a la vez). |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | para IA | Generación de cursos. |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | para voz | Narración. |
| `HEYGEN_API_KEY` | para avatar | Presentador IA. |

El entorno se detecta con `RAILWAY_ENVIRONMENT_NAME` (Railway lo define) o `ENVIRONMENT`.

## Despliegue (Railway)

- Imagen: `Dockerfile` (usuario no root, FFmpeg incluido). Arranca con `uvicorn app.main:app`; `main.py` se mantiene como alias (`uvicorn main:app` o `python main.py`).
- `railway.json` define el healthcheck en `/health`: si la configuración es inválida (p. ej. falta `JWT_SECRET`), el nuevo deploy no recibe tráfico y el anterior sigue activo.

## Videos de HeyGen

HeyGen apaga su API v1/v2 el 31-oct-2026. Al arrancar (y cada 30 min) la app copia a Storage los videos que aún apuntan a HeyGen. Pasada manual:

```bash
.venv/bin/python -m app.services.heygen_persist
```

## Logs

Todos los logs salen en JSON por stdout (un objeto por línea) con `ts`, `level`, `logger`, `message` (el nombre del evento) y campos estructurados; cada request lleva `x-request-id`. No se usa `print()`.
