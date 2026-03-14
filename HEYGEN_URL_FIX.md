# Fix: URLs de HeyGen Expiradas (403 Forbidden)

## Problema
Las URLs firmadas de AWS S3 que devuelve HeyGen expiran después de 24-48 horas, causando errores 403 Forbidden cuando los colaboradores intentan ver videos de cursos.

## Solución Implementada

### 1. Helper Function: `get_fresh_heygen_url(video_id)`
**Ubicación:** `routers/course_wizard.py`

Función que llama al API de HeyGen para obtener una URL fresca dado un `video_id`.

```python
def get_fresh_heygen_url(video_id: str) -> Optional[str]:
    """
    Obtiene una URL fresca de HeyGen para un video_id.
    Retorna None si falla o si el video no está completo.
    """
```

### 2. Formato Interno de Almacenamiento
**Antes:** `Module.video_url = "https://s3.amazonaws.com/...?signature=..."` (expira)  
**Ahora:** `Module.video_url = "heygen://video/{video_id}"` (referencia permanente)

### 3. Regeneración On-Demand

Los siguientes endpoints regeneran URLs automáticamente antes de devolverlas:

- ✅ `GET /enrollments/my-course/{id}` - Endpoint principal de colaboradores
- ✅ `GET /modules/{module_id}` - Vista individual de módulo
- ✅ `GET /modules/` - Listado de módulos
- ✅ `POST /courses/ai/check-video-status/{module_id}` - Check de status
- ✅ `POST /courses/ai/check-all-videos/{course_id}` - Batch check

## Archivos Modificados

1. **`routers/course_wizard.py`**
   - Agregado: `get_fresh_heygen_url()` helper
   - Modificado: `check_video_status()` - guarda formato `heygen://video/{id}`
   - Modificado: `check_all_videos()` - guarda formato `heygen://video/{id}`

2. **`routers/enrollments.py`**
   - Importado: `get_fresh_heygen_url`
   - Modificado: `my_course_detail()` - regenera URLs antes de devolver

3. **`routers/modules.py`**
   - Importado: `get_fresh_heygen_url`
   - Modificado: `get_module()` - regenera URL
   - Modificado: `list_modules()` - regenera URLs en listado

## Testing

### Verificar formato de guardado
```python
# Después de generar un video, verificar en DB:
SELECT id, title, video_url FROM modules WHERE video_url LIKE 'heygen://video/%';
```

### Probar endpoint de colaboradores
```bash
curl -H "Authorization: Bearer {token}" \
  https://colleague-backend-production.up.railway.app/enrollments/my-course/{course_id}
```

Verificar que `modules[].video_url` sea una URL válida de S3, no `heygen://video/...`

### Probar regeneración
```python
# 1. Crear módulo con video
# 2. Esperar 48h (o cambiar manualmente video_url a heygen://video/{id})
# 3. Llamar endpoint - debe devolver URL fresca
```

## Migración de URLs Existentes

Para migrar módulos con URLs expiradas, usar el script `migrate_heygen_urls.py`:

```bash
python migrate_heygen_urls.py
```

## Notas Técnicas

- Las URLs se regeneran **solo al devolver** en respuestas HTTP, nunca se modifica la DB automáticamente
- El formato `heygen://video/{id}` es nuestra convención interna
- Si `get_fresh_heygen_url()` falla, devuelve `None` y se usa la URL original
- Cada llamada al API de HeyGen consume una request, pero es necesario para evitar 403

## Próximos Pasos (Opcional)

- [ ] Agregar cache de URLs (Redis/Memcached) con TTL de ~12h
- [ ] Monitorear rate limits del API de HeyGen
- [ ] Agregar logs cuando las regeneraciones fallen
- [ ] Crear Pydantic validator custom para automatizar regeneración en schemas
