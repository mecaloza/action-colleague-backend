# Action Colleague — API

API de la plataforma de creación de cursos (con IA o con material propio). FastAPI + SQLAlchemy sobre Postgres (Supabase), desplegada en Railway.

## Estructura

```
app/
  main.py          create_app(): middlewares (proxy, CORS, logs), routers, health
  core/            config (variables de entorno), logging JSON, seguridad (JWT + bcrypt)
  api/deps.py      sesión de BD y usuario autenticado
  api/routes/      endpoints bajo /api/v1
  db/              modelos, sesión y arranque del esquema
  services/        integraciones (almacenamiento, IA, video)
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

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `DATABASE_URL` | en producción | Postgres de Supabase (pooler). En local, SQLite. |
| `JWT_SECRET` (o `SECRET_KEY`) | en producción | Mínimo 32 caracteres. La app no arranca en producción sin ella. |
| `ACCESS_TOKEN_MINUTES` | no | Vida del access token (por defecto 7 días, como antes). |
| `LOG_LEVEL` | no | `INFO` por defecto. |
| `CORS_ORIGINS` | no | Orígenes permitidos separados por coma. |
| `CORS_ORIGIN_REGEX` | no | Regex adicional de orígenes (p. ej. previews de Vercel). |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | para medios | Storage de Supabase (solo backend). |
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
