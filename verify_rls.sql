-- Verificar que RLS está habilitado en las tablas principales
SELECT 
  schemaname,
  tablename,
  rowsecurity as rls_enabled
FROM pg_tables 
WHERE schemaname = 'public' 
  AND tablename IN ('users', 'courses', 'modules', 'enrollments', 'user_videos', 'evaluations')
ORDER BY tablename;
