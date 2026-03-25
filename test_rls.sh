#!/bin/bash
# ================================================================
# 🔒 TEST RLS - Action Colleague
# ================================================================
# Este script verifica que Row Level Security esté funcionando
# correctamente en todas las tablas de Supabase.
#
# Proyecto: action-colleague (xfazeoeebrdswhppjksi)
# ================================================================

PROJECT_URL="https://xfazeoeebrdswhppjksi.supabase.co"
ANON_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhmYXplb2VlYnJkc3docHBqa3NpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MDg4NzY5MDksImV4cCI6MjAyNDQ1MjkwOX0.xwQKYRwPCWn3f5LFCDzGwXtGmCcPjZNf5eVpQFqH9hs"

# Colores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "================================================================"
echo "🔒 TESTING RLS - Action Colleague"
echo "================================================================"
echo ""

# ================================================================
# TEST 1: Sin autenticación NO debe devolver datos sensibles
# ================================================================
echo "TEST 1: Sin auth - Acceso a tabla users (DEBE FALLAR)"
echo "---------------------------------------------------------------"
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -H "apikey: $ANON_KEY" \
  -H "Authorization: Bearer $ANON_KEY" \
  "$PROJECT_URL/rest/v1/users?select=*")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ] && [ "$BODY" != "[]" ]; then
  echo -e "${RED}❌ FALLO: Se obtuvieron datos sin autenticación${NC}"
  echo "Response: $BODY"
else
  echo -e "${GREEN}✅ CORRECTO: RLS bloqueó acceso sin auth${NC}"
fi
echo ""

# ================================================================
# TEST 2: Sin autenticación NO debe devolver enrollments
# ================================================================
echo "TEST 2: Sin auth - Acceso a tabla enrollments (DEBE FALLAR)"
echo "---------------------------------------------------------------"
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -H "apikey: $ANON_KEY" \
  -H "Authorization: Bearer $ANON_KEY" \
  "$PROJECT_URL/rest/v1/enrollments?select=*")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ] && [ "$BODY" != "[]" ]; then
  echo -e "${RED}❌ FALLO: Se obtuvieron inscripciones sin autenticación${NC}"
  echo "Response: $BODY"
else
  echo -e "${GREEN}✅ CORRECTO: RLS bloqueó acceso a enrollments${NC}"
fi
echo ""

# ================================================================
# TEST 3: Courses publicados deben ser visibles (anon)
# ================================================================
echo "TEST 3: Sin auth - Acceso a courses publicados (DEBE FUNCIONAR)"
echo "---------------------------------------------------------------"
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -H "apikey: $ANON_KEY" \
  -H "Authorization: Bearer $ANON_KEY" \
  "$PROJECT_URL/rest/v1/courses?select=*&status=eq.published")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ]; then
  echo -e "${GREEN}✅ CORRECTO: Courses publicados son visibles${NC}"
  echo "Cursos encontrados: $(echo "$BODY" | jq '. | length')"
else
  echo -e "${YELLOW}⚠️  WARNING: No se pudieron obtener courses publicados${NC}"
  echo "Response: $BODY"
fi
echo ""

# ================================================================
# TEST 4: Verificar que service_role puede acceder a todo
# ================================================================
echo "TEST 4: Service Role - Acceso completo (DEBE FUNCIONAR)"
echo "---------------------------------------------------------------"
echo -e "${YELLOW}⚠️  Para este test necesitas SERVICE_ROLE_KEY${NC}"
echo "Ejecutar manualmente:"
echo "  export SERVICE_KEY='tu_service_key_aquí'"
echo "  curl -H \"apikey: \$SERVICE_KEY\" \\"
echo "       -H \"Authorization: Bearer \$SERVICE_KEY\" \\"
echo "       $PROJECT_URL/rest/v1/users"
echo ""

# ================================================================
# TEST 5: Verificar que user_videos está protegido
# ================================================================
echo "TEST 5: Sin auth - Acceso a user_videos (DEBE FALLAR)"
echo "---------------------------------------------------------------"
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -H "apikey: $ANON_KEY" \
  -H "Authorization: Bearer $ANON_KEY" \
  "$PROJECT_URL/rest/v1/user_videos?select=*")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ] && [ "$BODY" != "[]" ]; then
  echo -e "${RED}❌ FALLO: Se obtuvieron videos sin autenticación${NC}"
  echo "Response: $BODY"
else
  echo -e "${GREEN}✅ CORRECTO: RLS bloqueó acceso a user_videos${NC}"
fi
echo ""

# ================================================================
# RESUMEN
# ================================================================
echo "================================================================"
echo "📊 RESUMEN"
echo "================================================================"
echo ""
echo "Si todos los tests pasaron:"
echo "  ✅ RLS está habilitado correctamente"
echo "  ✅ Datos sensibles están protegidos"
echo "  ✅ Courses públicos son accesibles"
echo ""
echo "Siguiente paso:"
echo "  🔨 Verificar que el backend sigue funcionando con SERVICE_ROLE_KEY"
echo ""
echo "================================================================"
