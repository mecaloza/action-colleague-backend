-- ================================================================
-- 🔍 VERIFICAR ESTADO DE RLS - Action Colleague
-- ================================================================
-- Este script identifica qué tablas tienen RLS habilitado/deshabilitado
-- y cuántas políticas tienen definidas.
--
-- Ejecutar en: SQL Editor de Supabase
-- ================================================================

-- Tablas sin RLS habilitado
SELECT 
  schemaname,
  tablename,
  CASE 
    WHEN rowsecurity THEN '✅ RLS Enabled'
    ELSE '❌ RLS Disabled'
  END as rls_status
FROM pg_tables
LEFT JOIN pg_class ON pg_tables.tablename = pg_class.relname
WHERE schemaname = 'public'
  AND pg_class.relrowsecurity IS NOT NULL
ORDER BY tablename;

-- Conteo de políticas por tabla
SELECT 
  tablename,
  COUNT(*) as num_policies
FROM pg_policies
WHERE schemaname = 'public'
GROUP BY tablename
ORDER BY tablename;

-- Tablas sin ninguna política definida
SELECT 
  t.tablename,
  '⚠️ No policies defined' as status
FROM pg_tables t
LEFT JOIN pg_policies p ON t.tablename = p.tablename AND t.schemaname = p.schemaname
WHERE t.schemaname = 'public'
  AND p.tablename IS NULL
ORDER BY t.tablename;
