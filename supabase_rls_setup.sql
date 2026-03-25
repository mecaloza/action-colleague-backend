-- ================================================================
-- 🔒 SUPABASE RLS SETUP - Action Colleague
-- ================================================================
-- Este script habilita Row Level Security en todas las tablas
-- y define políticas de acceso seguras.
--
-- Proyecto: action-colleague (xfazeoeebrdswhppjksi)
-- Fecha: 2026-03-24
-- Author: Donowitz 🔨
-- ================================================================

-- ================================================================
-- PASO 1: HABILITAR RLS EN TODAS LAS TABLAS
-- ================================================================

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.courses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.modules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evaluation_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.enrollments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.module_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.certificates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.series ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scenes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.communications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_videos ENABLE ROW LEVEL SECURITY;

-- ================================================================
-- PASO 2: POLÍTICAS PARA TABLA USERS
-- ================================================================

-- Los usuarios solo pueden ver sus propios datos
CREATE POLICY "users_select_own" ON public.users
  FOR SELECT
  USING (auth.uid()::text = id::text);

-- Los usuarios pueden actualizar sus propios datos
CREATE POLICY "users_update_own" ON public.users
  FOR UPDATE
  USING (auth.uid()::text = id::text);

-- Solo admins pueden ver todos los usuarios
CREATE POLICY "users_select_admin" ON public.users
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users 
      WHERE id::text = auth.uid()::text 
      AND role = 'admin'
    )
  );

-- Service role tiene acceso completo
CREATE POLICY "users_service_role" ON public.users
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 3: POLÍTICAS PARA COURSES
-- ================================================================

-- Todos los autenticados pueden ver cursos publicados
CREATE POLICY "courses_select_published" ON public.courses
  FOR SELECT
  USING (status = 'published' OR auth.role() = 'service_role');

-- Creadores pueden ver sus propios cursos (incluso drafts)
CREATE POLICY "courses_select_own" ON public.courses
  FOR SELECT
  USING (created_by::text = auth.uid()::text);

-- Service role tiene acceso completo
CREATE POLICY "courses_service_role" ON public.courses
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 4: POLÍTICAS PARA MODULES
-- ================================================================

-- Los usuarios pueden ver módulos de cursos en los que están inscritos
CREATE POLICY "modules_select_enrolled" ON public.modules
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.enrollments e
      JOIN public.courses c ON e.course_id = c.id
      WHERE c.id = modules.course_id
      AND e.user_id::text = auth.uid()::text
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "modules_service_role" ON public.modules
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 5: POLÍTICAS PARA ENROLLMENTS
-- ================================================================

-- Los usuarios solo ven sus propias inscripciones
CREATE POLICY "enrollments_select_own" ON public.enrollments
  FOR SELECT
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden actualizar sus propias inscripciones
CREATE POLICY "enrollments_update_own" ON public.enrollments
  FOR UPDATE
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Service role tiene acceso completo
CREATE POLICY "enrollments_service_role" ON public.enrollments
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 6: POLÍTICAS PARA EVALUATIONS
-- ================================================================

-- Los usuarios pueden ver evaluaciones de módulos donde están inscritos
CREATE POLICY "evaluations_select_enrolled" ON public.evaluations
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.modules m
      JOIN public.courses c ON m.course_id = c.id
      JOIN public.enrollments e ON e.course_id = c.id
      WHERE m.id = evaluations.module_id
      AND e.user_id::text = auth.uid()::text
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "evaluations_service_role" ON public.evaluations
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 7: POLÍTICAS PARA EVALUATION_ATTEMPTS
-- ================================================================

-- Los usuarios solo ven sus propios intentos
CREATE POLICY "evaluation_attempts_select_own" ON public.evaluation_attempts
  FOR SELECT
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden crear sus propios intentos
CREATE POLICY "evaluation_attempts_insert_own" ON public.evaluation_attempts
  FOR INSERT
  WITH CHECK (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Service role tiene acceso completo
CREATE POLICY "evaluation_attempts_service_role" ON public.evaluation_attempts
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 8: POLÍTICAS PARA MODULE_PROGRESS
-- ================================================================

-- Los usuarios solo ven su propio progreso
CREATE POLICY "module_progress_select_own" ON public.module_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.enrollments e
      WHERE e.id = module_progress.enrollment_id
      AND e.user_id::text = auth.uid()::text
    )
    OR auth.role() = 'service_role'
  );

-- Los usuarios pueden actualizar su propio progreso
CREATE POLICY "module_progress_update_own" ON public.module_progress
  FOR UPDATE
  USING (
    EXISTS (
      SELECT 1 FROM public.enrollments e
      WHERE e.id = module_progress.enrollment_id
      AND e.user_id::text = auth.uid()::text
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "module_progress_service_role" ON public.module_progress
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 9: POLÍTICAS PARA CERTIFICATES
-- ================================================================

-- Los usuarios solo ven sus propios certificados
CREATE POLICY "certificates_select_own" ON public.certificates
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.enrollments e
      WHERE e.id = certificates.enrollment_id
      AND e.user_id::text = auth.uid()::text
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "certificates_service_role" ON public.certificates
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 10: POLÍTICAS PARA DOCUMENTS
-- ================================================================

-- Los usuarios solo ven sus propios documentos
CREATE POLICY "documents_select_own" ON public.documents
  FOR SELECT
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Service role tiene acceso completo
CREATE POLICY "documents_service_role" ON public.documents
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 11: POLÍTICAS PARA SERIES
-- ================================================================

-- Todos los autenticados pueden ver series publicadas
CREATE POLICY "series_select_published" ON public.series
  FOR SELECT
  USING (status = 'published' OR auth.role() = 'service_role');

-- Creadores pueden ver sus propias series
CREATE POLICY "series_select_own" ON public.series
  FOR SELECT
  USING (created_by::text = auth.uid()::text);

-- Service role tiene acceso completo
CREATE POLICY "series_service_role" ON public.series
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 12: POLÍTICAS PARA EPISODES
-- ================================================================

-- Los usuarios pueden ver episodios de series publicadas
CREATE POLICY "episodes_select_published_series" ON public.episodes
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.series s
      WHERE s.id = episodes.series_id
      AND (s.status = 'published' OR s.created_by::text = auth.uid()::text)
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "episodes_service_role" ON public.episodes
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 13: POLÍTICAS PARA SCENES
-- ================================================================

-- Los usuarios pueden ver escenas de episodios en series publicadas
CREATE POLICY "scenes_select_published_series" ON public.scenes
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.episodes ep
      JOIN public.series s ON ep.series_id = s.id
      WHERE ep.id = scenes.episode_id
      AND (s.status = 'published' OR s.created_by::text = auth.uid()::text)
    )
    OR auth.role() = 'service_role'
  );

-- Service role tiene acceso completo
CREATE POLICY "scenes_service_role" ON public.scenes
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 14: POLÍTICAS PARA COMMUNICATIONS
-- ================================================================

-- Todos los autenticados pueden ver comunicaciones
CREATE POLICY "communications_select_all" ON public.communications
  FOR SELECT
  TO authenticated
  USING (true);

-- Service role tiene acceso completo
CREATE POLICY "communications_service_role" ON public.communications
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 15: POLÍTICAS PARA REFRESH_TOKENS
-- ================================================================

-- Los usuarios solo pueden ver sus propios tokens
CREATE POLICY "refresh_tokens_select_own" ON public.refresh_tokens
  FOR SELECT
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden actualizar/borrar sus propios tokens
CREATE POLICY "refresh_tokens_update_own" ON public.refresh_tokens
  FOR UPDATE
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

CREATE POLICY "refresh_tokens_delete_own" ON public.refresh_tokens
  FOR DELETE
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Service role tiene acceso completo
CREATE POLICY "refresh_tokens_service_role" ON public.refresh_tokens
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- PASO 16: POLÍTICAS PARA USER_VIDEOS
-- ================================================================

-- Los usuarios solo ven sus propios videos
CREATE POLICY "user_videos_select_own" ON public.user_videos
  FOR SELECT
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden crear sus propios videos
CREATE POLICY "user_videos_insert_own" ON public.user_videos
  FOR INSERT
  WITH CHECK (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden actualizar sus propios videos
CREATE POLICY "user_videos_update_own" ON public.user_videos
  FOR UPDATE
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Los usuarios pueden borrar sus propios videos
CREATE POLICY "user_videos_delete_own" ON public.user_videos
  FOR DELETE
  USING (user_id::text = auth.uid()::text OR auth.role() = 'service_role');

-- Service role tiene acceso completo
CREATE POLICY "user_videos_service_role" ON public.user_videos
  FOR ALL
  USING (auth.role() = 'service_role');

-- ================================================================
-- ✅ SCRIPT COMPLETADO
-- ================================================================
-- 
-- Para ejecutar:
-- 1. Ir a: https://supabase.com/dashboard/project/xfazeoeebrdswhppjksi/editor
-- 2. Copiar y pegar este script completo
-- 3. Ejecutar
-- 4. Verificar que no hay errores
--
-- IMPORTANTE:
-- - Service role (backend) bypasa RLS automáticamente
-- - Las políticas requieren que auth.uid() esté presente
-- - Para testing sin auth, usar service_role key
-- ================================================================
