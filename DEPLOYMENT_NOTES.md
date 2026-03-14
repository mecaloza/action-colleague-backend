# 🚀 Deployment Notes - Fix URLs HeyGen Expiradas

**Fecha:** 2026-03-14  
**Developer:** Donowitz 🔨  
**Prioridad:** ALTA  

## 📋 Resumen Ejecutivo

Solucionado el problema de URLs expiradas (403 Forbidden) de videos HeyGen.

**Antes:** URLs firmadas de S3 se guardaban directamente → expiran en 24-48h  
**Ahora:** Se guarda referencia `heygen://video/{id}` → URLs se regeneran on-demand

## ✅ Cambios Implementados

### 1. Helper Function
- **Archivo:** `routers/course_wizard.py`
- **Función:** `get_fresh_heygen_url(video_id: str) -> Optional[str]`
- **Propósito:** Obtiene URL fresca del API de HeyGen

### 2. Modificación de Guardado
**Archivos modificados:**
- `routers/course_wizard.py`:
  - `check_video_status()` - línea ~838
  - `check_all_videos()` - línea ~920

**Cambio:**
```python
# Antes
mod.video_url = data.get("video_url", "")

# Ahora
mod.video_url = f"heygen://video/{video_id}"
```

### 3. Regeneración On-Demand
**Archivos modificados:**
- `routers/enrollments.py`:
  - `my_course_detail()` - línea ~193
  
- `routers/modules.py`:
  - `get_module()` - línea ~25
  - `list_modules()` - línea ~16

**Lógica:**
```python
if video_url and video_url.startswith("heygen://video/"):
    video_id = video_url.replace("heygen://video/", "")
    fresh_url = get_fresh_heygen_url(video_id)
    # Devolver fresh_url en respuesta
```

## 🧪 Testing Checklist

### Pre-Deploy
- [x] Verificar sintaxis Python (py_compile)
- [ ] Test unitario de `get_fresh_heygen_url()`
- [ ] Verificar HEYGEN_API_KEY en environment

### Post-Deploy
- [ ] Generar nuevo video y verificar formato `heygen://video/{id}` en DB
- [ ] Probar endpoint `/enrollments/my-course/{id}` - debe devolver URL válida
- [ ] Verificar que videos viejos con formato `heygen://video/{id}` devuelvan URLs frescas
- [ ] Confirmar con colaborador que videos reproducen sin 403

### Rollback Plan
Si hay problemas:
1. Revertir commits en los 3 archivos modificados
2. Deploy de versión anterior
3. Investigar logs del API de HeyGen

## 📦 Archivos Nuevos

1. **`HEYGEN_URL_FIX.md`** - Documentación técnica completa
2. **`migrate_heygen_urls.py`** - Script de migración (manual)
3. **`DEPLOYMENT_NOTES.md`** - Este archivo

## 🔄 Migración de Datos

Para módulos existentes con URLs expiradas:

```bash
# Opción 1: Script automático (requiere video_ids en metadatos)
python migrate_heygen_urls.py

# Opción 2: SQL manual si conoces los video_ids
UPDATE modules 
SET video_url = 'heygen://video/{VIDEO_ID}' 
WHERE id = {MODULE_ID};
```

## ⚠️ Consideraciones

- **Rate Limits:** Cada regeneración = 1 request al API de HeyGen
- **Performance:** Considerar cache (Redis) si hay muchas consultas
- **Fallback:** Si `get_fresh_heygen_url()` falla, devuelve URL original
- **Logging:** No se agregó logging explícito (considerar para v2)

## 🚦 Estado del Deploy

- [ ] Code review aprobado
- [ ] Tests passing
- [ ] Deployed a staging
- [ ] Tested en staging
- [ ] Deployed a production
- [ ] Verified en production

## 📞 Contacto

Problemas o dudas → Donowitz (backend) o Aldo Raine (líder técnico)

---

**Next Steps:**
1. Deploy a Railway (producción)
2. Monitorear logs por 24h
3. Ejecutar migración si hay módulos antiguos
4. Actualizar Notion con resultados
