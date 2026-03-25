# 🔒 RLS Deployment Guide - Action Colleague

**Proyecto:** action-colleague (xfazeoeebrdswhppjksi)  
**Fecha:** 2026-03-24  
**Autor:** Donowitz 🔨

---

## ⚠️ CONTEXTO CRÍTICO

Supabase reportó que **Row Level Security (RLS) está deshabilitado** en las tablas públicas, lo que significa que **TODOS los datos están completamente expuestos** a través de PostgREST API.

**Riesgo:**
- ✅ Cualquiera con el anon key puede leer TODA la base de datos
- ✅ PII, passwords, enrollments, certificados — TODO visible
- ✅ Sin autenticación, sin autorización

**Este deployment es CRÍTICO y debe ejecutarse YA.**

---

## 📋 CHECKLIST DE DEPLOYMENT

### ✅ Pre-Flight

- [ ] Leer este documento completo
- [ ] Acceder a Supabase Dashboard
- [ ] Tener `.env` con `SUPABASE_SERVICE_KEY` disponible
- [ ] Avisar al equipo que se va a aplicar cambio crítico de seguridad

### 🔍 PASO 1: Verificar Estado Actual

1. Ir a: https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi/editor
2. Abrir archivo: `check_rls_status.sql`
3. Copiar todo el contenido y ejecutar en SQL Editor
4. Revisar resultados:
   - ¿Cuántas tablas tienen RLS disabled?
   - ¿Cuántas políticas existen actualmente?

**Resultado esperado:** La mayoría de tablas deberían mostrar "❌ RLS Disabled"

---

### 🔨 PASO 2: Habilitar RLS + Aplicar Políticas

**⚠️ IMPORTANTE:** Este cambio es **NO DESTRUCTIVO** pero **BLOQUEARÁ ACCESO** inmediato a los datos desde PostgREST sin autenticación válida.

1. Ir a: https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi/editor
2. Abrir archivo: `supabase_rls_setup.sql`
3. Copiar **TODO** el contenido (15 tablas + políticas)
4. Pegar en SQL Editor
5. **REVISAR** que el script es correcto
6. Click en **"Run"**
7. Verificar que NO hay errores en output

**Duración estimada:** 5-10 segundos

**¿Qué hace este script?**
- Habilita RLS en 15 tablas
- Crea ~50 políticas de seguridad
- Define acceso basado en `auth.uid()` (usuario autenticado)
- Permite acceso completo a `service_role` (backend)

---

### 🧪 PASO 3: Testing

#### Test A: Verificar RLS Básico (Automático)

```bash
cd /Users/lukeskywalker/.openclaw/workspace/projects/action-colleague/backend
./test_rls.sh
```

**Resultado esperado:**
- ✅ Test 1: Bloqueado acceso a `users` sin auth
- ✅ Test 2: Bloqueado acceso a `enrollments` sin auth
- ✅ Test 3: Courses públicos visibles
- ✅ Test 5: Bloqueado acceso a `user_videos` sin auth

#### Test B: Verificar Backend Sigue Funcionando

El backend usa `SUPABASE_SERVICE_KEY` que **bypasa RLS automáticamente**.

1. Verificar `.env`:
   ```bash
   cat .env | grep SUPABASE_SERVICE_KEY
   ```

2. Hacer un request al backend (local o Railway):
   ```bash
   # Local
   curl http://localhost:8000/courses

   # Railway
   curl https://colleague-backend-production.up.railway.app/courses
   ```

3. Verificar que devuelve datos correctamente.

**Si falla:** El backend probablemente está usando anon key en lugar de service key.

---

### 🔧 PASO 4: Verificar Configuración del Backend

Revisar que el backend usa `SUPABASE_SERVICE_KEY`:

```bash
cd /Users/lukeskywalker/.openclaw/workspace/projects/action-colleague/backend
grep -r "SUPABASE" *.py routers/*.py
```

**¿Qué buscar?**
- Backend debe usar `os.getenv("SUPABASE_SERVICE_KEY")` para operaciones críticas
- **NO** debe usar `SUPABASE_ANON_KEY` para queries de escritura

**Archivos clave:**
- `database.py` — Ya usa la URL con credenciales service
- Cualquier cliente Supabase directo debe usar service key

---

### 📊 PASO 5: Monitoreo Post-Deploy

#### A. Verificar que RLS está activo

```sql
-- Ejecutar en SQL Editor
SELECT 
  tablename,
  CASE WHEN rowsecurity THEN '✅' ELSE '❌' END as rls_enabled
FROM pg_tables
JOIN pg_class ON pg_tables.tablename = pg_class.relname
WHERE schemaname = 'public'
ORDER BY tablename;
```

**Resultado esperado:** Todas las tablas con ✅

#### B. Verificar políticas aplicadas

```sql
-- Ejecutar en SQL Editor
SELECT 
  tablename,
  COUNT(*) as total_policies
FROM pg_policies
WHERE schemaname = 'public'
GROUP BY tablename
ORDER BY tablename;
```

**Resultado esperado:** Cada tabla debe tener 2-5 políticas

#### C. Test manual con anon key

```bash
# Esto DEBE devolver vacío o error 403
curl -H "apikey: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." \
  https://xfazeoeebrdswhppjksi.supabase.co/rest/v1/users
```

**Resultado esperado:** `[]` (vacío) o error de permisos

---

### 🚨 ROLLBACK (Si algo falla)

**Opción 1: Deshabilitar RLS temporalmente**

```sql
-- SOLO usar en EMERGENCIA
ALTER TABLE public.users DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.enrollments DISABLE ROW LEVEL SECURITY;
-- ... etc
```

**Opción 2: Eliminar políticas problemáticas**

```sql
-- Ver políticas
SELECT * FROM pg_policies WHERE schemaname = 'public' AND tablename = 'nombre_tabla';

-- Eliminar política específica
DROP POLICY "nombre_politica" ON public.nombre_tabla;
```

---

### ✅ CRITERIOS DE ÉXITO

- [ ] RLS habilitado en todas las tablas (check_rls_status.sql confirma)
- [ ] Tests automáticos (test_rls.sh) pasan
- [ ] Backend local funciona correctamente
- [ ] Backend en Railway funciona correctamente
- [ ] Requests sin autenticación NO devuelven datos sensibles
- [ ] Courses públicos siguen siendo visibles
- [ ] Frontend puede autenticarse y obtener datos de usuarios

---

### 📝 NOTAS IMPORTANTES

#### ¿Por qué service_role bypasa RLS?

- El backend **necesita** acceso completo para operaciones administrativas
- Supabase distingue entre:
  - `anon` role → usuarios no autenticados (RLS aplicado)
  - `authenticated` role → usuarios con JWT (RLS aplicado)
  - `service_role` → backend/admin (RLS bypasseado)

#### ¿Qué pasa con las operaciones actuales?

- **Frontend:** Debe autenticarse con JWT válido
- **Backend:** Usa service key, sigue funcionando igual
- **PostgREST público:** Bloqueado para datos sensibles

#### ¿Cómo funciona auth.uid()?

```sql
-- En una política:
USING (auth.uid()::text = user_id::text)

-- auth.uid() devuelve el UUID del usuario autenticado
-- Si no hay JWT válido, auth.uid() es NULL
-- Por lo tanto, la condición falla y se bloquea acceso
```

---

### 🔗 RECURSOS

- **Supabase Dashboard:** https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi
- **SQL Editor:** https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi/editor
- **RLS Docs:** https://supabase.com/docs/guides/auth/row-level-security
- **Railway Backend:** https://colleague-backend-production.up.railway.app

---

### 🎯 SIGUIENTE PASO

Después de este deployment:

1. **Refinamiento de políticas:** Ajustar según casos de uso reales
2. **Audit logs:** Habilitar logging de queries rechazadas
3. **Testing de edge cases:** Managers viendo reportes, admins creando cursos, etc.
4. **Documentation:** Actualizar README con nuevas políticas de seguridad

---

## 🔨 READY TO EXECUTE?

**Comando rápido:**

```bash
# 1. Verificar estado
# Copiar check_rls_status.sql → Supabase SQL Editor → Run

# 2. Aplicar RLS
# Copiar supabase_rls_setup.sql → Supabase SQL Editor → Run

# 3. Test
cd /Users/lukeskywalker/.openclaw/workspace/projects/action-colleague/backend
./test_rls.sh

# 4. Verificar backend
curl https://colleague-backend-production.up.railway.app/courses
```

**GO GO GO** 🔨🔒
