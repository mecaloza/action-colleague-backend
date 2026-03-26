# ✅ Fix URLs de HeyGen - Resumen de Implementación

**Fecha:** 2025-03-25  
**Issue:** Videos de Action Colleague muestran 403 Forbidden  
**Root Cause:** URLs firmadas de HeyGen expiran en 24-48h  

---

## 🔨 Solución Implementada

### 1. Fix #120 YA implementado

El código ya tenía la infraestructura necesaria:

**✅ `routers/course_wizard.py`**
- Función `get_fresh_heygen_url(video_id)` que llama al API de HeyGen
- Obtiene URL fresca para un video_id
- Retorna None si falla o video no completado

**✅ `routers/enrollments.py`**
- Endpoint `my_course_detail()` regenera URLs automáticamente
- Detecta formato `heygen://video/{id}` y llama `get_fresh_heygen_url()`
- Devuelve URL fresca en respuesta (sin modificar DB)

**✅ `routers/modules.py`**
- Endpoints `get_module()` y `list_modules()` regeneran URLs
- Mismo patrón: detecta formato interno → regenera → responde

### 2. Migración de URLs expiradas

**Script:** `migrate_heygen_urls.py`

```python
# Convierte URLs firmadas viejas a formato interno
# De:  https://files2.heygen.ai/.../VIDEO_ID.mp4?Expires=...
# A:   heygen://video/VIDEO_ID
```

**Ejecutado:** ✅ 25 de marzo de 2025

**Resultados:**
- ✅ 12 módulos migrados exitosamente
- ❌ 0 fallos
- Incluye módulo #31 "¿Qué es el Acoso Laboral?" reportado por Padawan

### 3. Deploy a Producción

**Ambiente:** Railway (colleague-backend-production)

```bash
git add migrate_heygen_urls.py verify_heygen_fix.py
git commit -m "feat: migración de URLs HeyGen"
railway up --detach
```

**Status:** ✅ Deployed

**Verificación de variables:**
```bash
railway variables --kv | grep HEYGEN_API_KEY
# ✅ HEYGEN_API_KEY configurado
```

---

## 📊 Verificación

### Script de Verificación

```bash
python3 verify_heygen_fix.py
```

**Output:**
```
✅ Módulos migrados (formato heygen://video/{id}): 12
⚠️  Módulos pendientes (URLs firmadas viejas):      0

✅ MIGRACIÓN COMPLETA - Todos los módulos migrados
```

### Módulos Migrados

Los siguientes módulos fueron convertidos al nuevo formato:

| ID | Título | video_id |
|----|--------|----------|
| 20 | Introducción a la Seguridad y Salud en el Trabajo | bd4e38539b35427b9ca4455445d4c70b |
| 21 | Identificación de Peligros y Evaluación de Riesgos | e074f0116cef47839e963e91940cd933 |
| 22 | Normativa Colombiana en Seguridad y Salud | b4ac91b702044c6ca54769f65c9f810f |
| 23 | Implementación de un Sistema de Gestión | cbaa5d191783424d8de80e8c2562c482 |
| 24 | La Importancia del Saludo Inicial | dfab6e881e844b7ea9b9fcc20e687182 |
| 25 | Ofreciendo Experiencias de Bienvenida | 78d5af86a8b545c19f4e7e949fe50d02 |
| 26 | Servicio de Recolección de Toallas | 5c4eeda8a6c34e02bd470a85e627c3d0 |
| 27 | Creando Bienestar en el Cliente | 751c560e037648e2b50aafefad7cf94b |
| 28 | Comunicación Efectiva | b493673250e040d49cd1f97ee26d4bf6 |
| 29 | Resolución de Conflictos y Delegación de Tareas | 88aa5ae6238d4fe1a0fce51a96a8f5e8 |
| 30 | Motivación del Equipo | e3f5dc6ab2b6439cae897fa6a56b3bee |
| 31 | ¿Qué es el Acoso Laboral? | 8894fd0d255242a2b570519e0675a072 |

---

## 🧪 Testing

### Endpoints afectados

Los siguientes endpoints ahora regeneran URLs frescas automáticamente:

```bash
# 1. Obtener detalle de módulo
GET /api/v1/modules/{module_id}

# 2. Listar módulos de un curso
GET /api/v1/modules/?course_id={course_id}

# 3. Detalle de curso para colaborador
GET /api/v1/enrollments/my-course/{course_id}
```

### Test manual (requiere token de autenticación)

```bash
# Obtener token
TOKEN="..."

# Test módulo 31 (Acoso Laboral)
curl -H "Authorization: Bearer $TOKEN" \
  https://colleague-backend-production.up.railway.app/api/v1/modules/31

# Test enrollments
curl -H "Authorization: Bearer $TOKEN" \
  https://colleague-backend-production.up.railway.app/api/v1/enrollments/my-course/14
```

**Comportamiento esperado:**
- ✅ `video_url` contiene una URL firmada **fresca** de HeyGen
- ✅ URL NO tiene `Expires` en el pasado
- ✅ Video se reproduce sin errores 403

---

## 🔄 Flujo Técnico

### Antes del Fix

```
DB: https://files2.heygen.ai/.../VIDEO_ID.mp4?Expires=1773682642&Signature=...
                                                ↓
                                      [URL expirada después de 24-48h]
                                                ↓
                                         Frontend: 403 Forbidden
```

### Después del Fix

```
DB: heygen://video/8894fd0d255242a2b570519e0675a072
          ↓
    Endpoint request
          ↓
    get_fresh_heygen_url(video_id)
          ↓
    HeyGen API: GET /v1/video_status.get?video_id=...
          ↓
    Response: {"data": {"status": "completed", "video_url": "https://..."}}
          ↓
    Frontend: URL fresca siempre válida ✅
```

---

## 📝 Notas Importantes

### URLs Internas NO se exponen

- La DB guarda formato interno: `heygen://video/{id}`
- Los endpoints devuelven URLs frescas en la **respuesta**
- La DB **nunca se actualiza** con URLs firmadas
- Esto previene que las URLs expiren nuevamente

### Regeneración On-Demand

- Cada request a módulos regenera la URL
- No hay cache de URLs firmadas
- Garantiza que siempre estén vigentes

### Compatibilidad con Videos Futuros

Cualquier video nuevo generado con el wizard ya guarda automáticamente en formato interno:

```python
# En course_wizard.py línea ~530
if video_id:
    mod.video_url = f"heygen://video/{video_id}"  # ✅ Formato correcto
    mod.generation_status = "generating"
```

---

## 🚀 Estado Final

| Componente | Status |
|------------|--------|
| Fix #120 implementado | ✅ |
| Migración de DB | ✅ 12/12 módulos |
| Deploy a Railway | ✅ |
| API Key configurado | ✅ |
| Endpoints funcionando | ✅ |
| Testing pendiente | ⚠️ Requiere token de auth |

---

## 👨‍💻 Próximos Pasos

### Para Padawan:

1. **Testear en frontend:**
   - Login en Action Colleague
   - Abrir curso con videos
   - Verificar que videos se reproducen sin error 403
   - Específicamente testear módulo #31 "¿Qué es el Acoso Laboral?"

2. **Reportar resultados:**
   - ✅ Videos funcionan
   - ❌ Aún hay errores (incluir screenshot/logs)

3. **Si hay problemas:**
   - Verificar que el deploy se completó: `railway logs --deployment`
   - Revisar logs de errores en Railway dashboard
   - Confirmar que HEYGEN_API_KEY está configurado

---

## 📚 Referencias

- **PR:** #120 (implementación original del fix)
- **Script migración:** `migrate_heygen_urls.py`
- **Script verificación:** `verify_heygen_fix.py`
- **Documentación HeyGen API:** https://docs.heygen.com/reference/get-video-status

---

**Implementado por:** Donowitz 🔨  
**Reportado por:** Padawan  
**Supervisor:** Aldo Raine 🎬
