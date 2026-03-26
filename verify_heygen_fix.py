#!/usr/bin/env python3
"""
Script de verificación: Fix de URLs de HeyGen

Verifica que:
1. Los módulos tengan URLs en formato heygen://video/{id}
2. La función get_fresh_heygen_url() funcione correctamente
3. Los endpoints regeneren URLs frescas

Uso:
    python3 verify_heygen_fix.py
"""

from database import SessionLocal
from models import Module
from routers.course_wizard import get_fresh_heygen_url

def verify_migration():
    """Verifica que la migración se haya aplicado correctamente."""
    db = SessionLocal()
    
    # Verificar módulos con formato nuevo
    migrated = db.query(Module).filter(
        Module.video_url.like('heygen://video/%')
    ).all()
    
    # Verificar módulos con URLs firmadas viejas
    old_urls = db.query(Module).filter(
        Module.video_url.like('%files2.heygen.ai%')
    ).all()
    
    print("="*60)
    print("📊 VERIFICACIÓN DE MIGRACIÓN DE URLs HEYGEN")
    print("="*60)
    print()
    print(f"✅ Módulos migrados (formato heygen://video/{{id}}): {len(migrated)}")
    print(f"⚠️  Módulos pendientes (URLs firmadas viejas):      {len(old_urls)}")
    print()
    
    if old_urls:
        print("⚠️  MÓDULOS PENDIENTES DE MIGRAR:")
        for mod in old_urls[:5]:
            print(f"   - Módulo {mod.id}: {mod.title}")
            print(f"     URL: {mod.video_url[:80]}...")
        print()
    
    # Test de regeneración con un módulo migrado
    if migrated:
        test_module = migrated[0]
        video_id = test_module.video_url.replace("heygen://video/", "")
        
        print(f"🧪 TEST DE REGENERACIÓN:")
        print(f"   Módulo: {test_module.title}")
        print(f"   video_id: {video_id}")
        print(f"   Llamando a get_fresh_heygen_url()...")
        
        try:
            fresh_url = get_fresh_heygen_url(video_id)
            if fresh_url:
                print(f"   ✅ URL regenerada exitosamente")
                print(f"   URL: {fresh_url[:100]}...")
            else:
                print(f"   ⚠️  get_fresh_heygen_url() retornó None")
                print(f"   (Esto puede ser normal si HEYGEN_API_KEY no está configurado)")
        except Exception as e:
            print(f"   ❌ Error: {e}")
    
    print()
    print("="*60)
    print("📋 RESUMEN:")
    print("="*60)
    
    if len(migrated) > 0 and len(old_urls) == 0:
        print("✅ MIGRACIÓN COMPLETA - Todos los módulos migrados")
    elif len(migrated) > 0:
        print(f"⚠️  MIGRACIÓN PARCIAL - {len(old_urls)} módulos pendientes")
    else:
        print("❌ MIGRACIÓN NO APLICADA - Ejecutar migrate_heygen_urls.py")
    
    print()
    print("💡 PRÓXIMOS PASOS:")
    if old_urls:
        print("   1. Ejecutar: python3 migrate_heygen_urls.py")
        print("   2. Deploy a Railway con: railway up --detach")
    else:
        print("   1. ✅ Migración aplicada en DB")
        print("   2. Verificar que HEYGEN_API_KEY esté en variables de Railway")
        print("   3. Testear endpoints:")
        print("      GET /api/v1/modules/{id}")
        print("      GET /api/v1/enrollments/my-course/{course_id}")
    print("="*60)
    
    db.close()

if __name__ == "__main__":
    verify_migration()
