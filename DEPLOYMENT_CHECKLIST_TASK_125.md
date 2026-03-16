# 🚀 Deployment Checklist - Task #125 Video Upload

**Developer:** Donowitz 🔨  
**Status:** ✅ CÓDIGO COMPLETADO - Listo para deploy  
**Operations Task:** #130  
**Date:** 2026-03-15

---

## ✅ Completado

- [x] Modelo `UserVideo` creado en `models.py`
- [x] Schemas `VideoUploadResponse` y `UserVideoOut` en `schemas.py`
- [x] Router `/api/v1/videos` creado con 3 endpoints
- [x] Router registrado en `main.py`
- [x] Auto-migración de tabla `user_videos` en `database.py`
- [x] Validaciones implementadas (formato, tamaño, duración)
- [x] Integración con Supabase Storage
- [x] Commit al repo: `18ca1b7`
- [x] Documentación creada: `TASK_125_VIDEO_UPLOAD.md`
- [x] Tarea registrada en Operations Dashboard (#130)

---

## 🔧 Acciones Requeridas por Padawan

### 1. Configurar Variables de Entorno en Railway

**Railway Dashboard → colleague-backend-production → Variables:**

Agregar estas 2 variables nuevas:

```
SUPABASE_URL=https://xfazeoeebrdswhppjksi.supabase.co
SUPABASE_SERVICE_KEY=<obtener-de-supabase-dashboard>
```

**Dónde obtener el service_role key:**
1. Ve a https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi
2. Settings → API
3. Copia el **service_role key** (NO el anon key)

### 2. Crear Bucket en Supabase Storage

**Supabase Dashboard → Storage:**

1. Click "New Bucket"
2. Name: `user-videos`
3. Public: ✅ **YES** (importante para que Stiglitz pueda mostrar videos)
4. File size limit: 500 MB (opcional, ya validamos en backend)
5. Save

**Verificar:**
- Bucket debe ser público
- URL pública: `https://xfazeoeebrdswhppjksi.supabase.co/storage/v1/object/public/user-videos/`

### 3. Deploy a Railway

**Opción A: Auto-deploy (si ya configuraste GitHub integration)**
```bash
# Ya hice el commit, Railway debería auto-deployar
git log --oneline -1
# 18ca1b7 feat: add video upload endpoint (Task #125)
```

**Opción B: Manual deploy**
```bash
cd /Users/lukeskywalker/.openclaw/workspace/projects/action-colleague/backend
git push origin main
```

Railway detectará los cambios y re-deployará automáticamente.

### 4. Verificar Deployment

**Después del deploy, verificar:**

1. **Health check:**
   ```bash
   curl https://colleague-backend-production.up.railway.app/health
   # Expected: {"status":"ok"}
   ```

2. **Tabla creada:**
   - Conectar a Supabase → SQL Editor
   - Ejecutar: `SELECT * FROM user_videos LIMIT 1;`
   - Debería existir la tabla (vacía)

3. **Endpoint disponible:**
   ```bash
   curl https://colleague-backend-production.up.railway.app/api/v1/videos/upload \
     -X OPTIONS
   # Debería retornar CORS headers (200 OK)
   ```

### 5. Test de Upload (Opcional pero recomendado)

```bash
# 1. Login para obtener token
curl -X POST https://colleague-backend-production.up.railway.app/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"your-password"}'

# Guardar el access_token

# 2. Subir un video de prueba
curl -X POST https://colleague-backend-production.up.railway.app/api/v1/videos/upload \
  -H "Authorization: Bearer <access_token>" \
  -F "file=@test.webm" \
  -F "duration=10"

# Expected: 200 OK con VideoUploadResponse
```

---

## 📋 Validaciones Post-Deploy

**Checklist:**
- [ ] Variables de entorno configuradas en Railway
- [ ] Bucket `user-videos` creado en Supabase
- [ ] Deploy exitoso en Railway (sin errores)
- [ ] Tabla `user_videos` existe en base de datos
- [ ] Endpoint `/api/v1/videos/upload` responde
- [ ] Test de upload funciona (opcional)

---

## 🎯 Siguiente Paso: Integración Frontend

**Para Stiglitz 🎯:**

Una vez que confirmes el deploy, notifica a Stiglitz que puede integrar el endpoint en su VideoRecorder component.

**Endpoint de producción:**
```
POST https://colleague-backend-production.up.railway.app/api/v1/videos/upload
```

**Documentación para FE:**
- Ver `TASK_125_VIDEO_UPLOAD.md` sección "Integración Frontend"
- Ejemplo de código TypeScript incluido

---

## 🐛 Troubleshooting

**Si el deploy falla:**
1. Revisar logs en Railway Dashboard
2. Verificar que las variables de entorno estén configuradas
3. Ejecutar `railway logs` para ver errores en tiempo real

**Si los uploads fallan:**
1. Verificar que `SUPABASE_SERVICE_KEY` sea el correcto
2. Confirmar que el bucket `user-videos` existe y es público
3. Revisar logs del backend para ver el error específico

---

## 📊 Métricas de Éxito

**Cuando todo esté funcionando:**
- ✅ Endpoint responde con 200 OK
- ✅ Videos se guardan en Supabase Storage
- ✅ URLs públicas son accesibles
- ✅ Metadata se guarda en DB correctamente
- ✅ Validaciones de tamaño/formato funcionan

---

## 🎬 Conclusión

**Estado actual:**
- Código: ✅ Implementado y commiteado
- Testing local: ✅ Compila sin errores
- Documentación: ✅ Completa
- Operations: ✅ Tarea #130 marcada como DONE

**Pendiente:**
- ⏳ Deploy a Railway (requiere configuración de Padawan)
- ⏳ Test en producción
- ⏳ Notificar a Stiglitz para integración FE

---

**Implementado por:** Donowitz 🔨  
**Commit:** `18ca1b7`  
**Operations Task:** #130  
**Ready for:** Production Deployment 🚀
