# Task #125 - Video Upload Endpoint ✅

**Status:** IMPLEMENTADO - Listo para testing

**Developer:** Donowitz 🔨  
**Date:** 2026-03-15  
**Prioridad:** High

---

## 📦 Archivos Modificados/Creados

✅ **models.py** - Agregado modelo `UserVideo`  
✅ **schemas.py** - Agregados schemas `VideoUploadResponse` y `UserVideoOut`  
✅ **routers/videos.py** - Nuevo router con 3 endpoints  
✅ **main.py** - Router registrado en `/api/v1/videos`  
✅ **database.py** - Auto-migración para tabla `user_videos`

---

## 🛠️ Implementación

### Modelo UserVideo

```python
class UserVideo(Base):
    __tablename__ = "user_videos"
    
    id = Column(String(36), primary_key=True)  # UUID
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    storage_url = Column(String(500), nullable=False)
    duration = Column(Integer)  # seconds
    file_size = Column(Integer)  # bytes
    format = Column(String(20), default="webm")  # webm, mp4
    created_at = Column(DateTime)
    status = Column(String(20), default="uploaded")
```

### Endpoints Disponibles

#### 1. POST /api/v1/videos/upload
**Descripción:** Upload video grabado por usuario

**Request:**
- Multipart form-data
- `file`: UploadFile (required)
- `module_id`: int (optional)
- `duration`: int (optional, seconds)

**Validaciones:**
- ✅ MIME type: video/webm o video/mp4
- ✅ Tamaño máximo: 500MB
- ✅ Duración máxima: 1800s (30 min)
- ✅ UUID único generado automáticamente

**Response:**
```json
{
  "video_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_url": "https://supabase.co/storage/v1/object/public/user-videos/550e8400.webm",
  "status": "uploaded",
  "duration": 120,
  "file_size": 12582912,
  "format": "webm"
}
```

#### 2. GET /api/v1/videos/{video_id}
**Descripción:** Obtener metadata de un video

**Response:** `UserVideoOut`

#### 3. GET /api/v1/videos?module_id={id}
**Descripción:** Listar videos
- Si `module_id`: videos de ese módulo
- Sin `module_id`: videos del usuario actual

**Response:** `List[UserVideoOut]`

---

## 🔧 Setup Requerido

### Variables de Entorno (.env)

Necesitas agregar estas variables para que Supabase Storage funcione:

```bash
# Agregar a .env
SUPABASE_URL=https://xfazeoeebrdswhppjksi.supabase.co
SUPABASE_SERVICE_KEY=<service_role_key>
```

**⚠️ IMPORTANTE:**
- `SUPABASE_SERVICE_KEY` debe ser el **service_role key**, NO el anon key
- Lo encuentras en Supabase Dashboard → Settings → API

### Crear Bucket en Supabase

El código crea automáticamente el bucket `user-videos` si no existe, pero recomiendo crearlo manualmente:

1. Ve a Supabase Dashboard → Storage
2. Click "New Bucket"
3. Name: `user-videos`
4. Public: ✅ Yes
5. Save

---

## 🧪 Testing Local

### 1. Iniciar servidor local

```bash
cd /Users/lukeskywalker/.openclaw/workspace/projects/action-colleague/backend
uvicorn main:app --reload --port 8001
```

### 2. Obtener token de autenticación

```bash
# Login para obtener token
curl -X POST http://localhost:8001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"password"}'

# Guardar el access_token de la respuesta
export TOKEN="<access_token>"
```

### 3. Test de upload de video

```bash
# Crear un video de prueba (o usa uno existente)
# Para test rápido, puedes usar cualquier archivo .webm o .mp4

curl -X POST http://localhost:8001/api/v1/videos/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@test_video.webm" \
  -F "module_id=1" \
  -F "duration=120"
```

**Respuesta esperada (200 OK):**
```json
{
  "video_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_url": "https://xfazeoeebrdswhppjksi.supabase.co/storage/v1/object/public/user-videos/550e8400-e29b-41d4-a716-446655440000.webm",
  "status": "uploaded",
  "duration": 120,
  "file_size": 1234567,
  "format": "webm"
}
```

### 4. Verificar en Supabase

1. Ve a Supabase → Storage → user-videos
2. Deberías ver el archivo `<uuid>.webm`
3. Click para ver la URL pública

### 5. Test de errores

```bash
# Test archivo muy grande (>500MB) - debe fallar
curl -X POST http://localhost:8001/api/v1/videos/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@huge_video.mp4"

# Expected: 400 "File too large"

# Test formato inválido - debe fallar
curl -X POST http://localhost:8001/api/v1/videos/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@document.pdf"

# Expected: 400 "Invalid format"
```

---

## 🚀 Deployment

### Railway (Production)

1. **Variables de entorno:**
   - Agregar `SUPABASE_URL` y `SUPABASE_SERVICE_KEY` en Railway Dashboard

2. **Deploy:**
   ```bash
   git add .
   git commit -m "feat: add video upload endpoint (Task #125)"
   git push origin main
   ```

3. **Verificar:**
   - Railway auto-deploya
   - Tabla `user_videos` se crea automáticamente en primer startup
   - Endpoint disponible en `https://colleague-backend-production.up.railway.app/api/v1/videos/upload`

---

## 🔗 Integración Frontend

**Para Stiglitz 🎯:**

El endpoint está listo para integrar con tu VideoRecorder component.

**Ejemplo de uso (fetch API):**

```typescript
async function uploadVideo(blob: Blob, moduleId: number, duration: number) {
  const formData = new FormData();
  formData.append('file', blob, 'recording.webm');
  formData.append('module_id', moduleId.toString());
  formData.append('duration', duration.toString());

  const response = await fetch(
    'https://colleague-backend-production.up.railway.app/api/v1/videos/upload',
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${accessToken}`,
      },
      body: formData,
    }
  );

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail);
  }

  return await response.json(); // VideoUploadResponse
}
```

---

## ✅ Checklist de Entregables

- [x] Modelo `UserVideo` en DB
- [x] Schemas request/response
- [x] Endpoint `/videos/upload` funcional
- [x] Validaciones básicas (formato, tamaño, duración)
- [x] Storage integration (Supabase)
- [x] Auto-migración de tabla
- [x] Documentación de testing
- [x] Ejemplos de curl

---

## 📋 Próximos Pasos (No incluidos en MVP)

**Task #126 (Semana 2):**
- Thumbnail generation (FFmpeg)
- Conversión de formatos
- Procesamiento asíncrono

**Mejoras futuras:**
- Progress bar durante upload
- Compresión automática
- Multi-resolution encoding (HLS/DASH)

---

## 🐛 Troubleshooting

### Error: "Supabase Storage not configured"
→ Asegúrate de tener `SUPABASE_URL` y `SUPABASE_SERVICE_KEY` en `.env`

### Error: "Failed to upload to Supabase"
→ Verifica que el service_role key sea correcto
→ Revisa que el bucket `user-videos` exista y sea público

### Error: "File too large"
→ El límite es 500MB. Para videos más grandes, contactar a Padawan para aumentar límite.

---

**Implementado por:** Donowitz 🔨  
**Aprobado por:** Aldo Raine 🎬 (pendiente)  
**Ready for:** Testing + Deploy to Production
