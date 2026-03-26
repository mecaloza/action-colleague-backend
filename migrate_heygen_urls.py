#!/usr/bin/env python3
"""
Migración de URLs de HeyGen expiradas a formato heygen://video/{id}

Convierte URLs firmadas que expiran en 24-48h a referencias internas
que se regeneran dinámicamente en cada request.
"""

from sqlalchemy.orm import Session
from database import SessionLocal
from models import Module
import re

def migrate_urls():
    db = SessionLocal()
    
    # Buscar módulos con URLs de HeyGen expiradas (firmadas)
    modules = db.query(Module).filter(
        Module.video_url.like('%files2.heygen.ai%')
    ).all()
    
    print(f"🔍 Encontrados {len(modules)} módulos con URLs de HeyGen firmadas")
    
    migrated = 0
    failed = []
    
    for mod in modules:
        # Extraer video_id de la URL firmada
        # Formato típico: .../avatar_tmp/xxx/VIDEO_ID.mp4?Expires=...
        # El video_id es un hash de 32 caracteres hexadecimales
        match = re.search(r'/([a-f0-9]{32})\.mp4', mod.video_url)
        
        if match:
            video_id = match.group(1)
            old_url = mod.video_url
            new_url = f"heygen://video/{video_id}"
            
            print(f"\n📦 Módulo {mod.id}: {mod.title}")
            print(f"   Viejo: {old_url[:100]}...")
            print(f"   Nuevo: {new_url}")
            
            mod.video_url = new_url
            migrated += 1
        else:
            print(f"\n⚠️  Módulo {mod.id}: No se pudo extraer video_id de {mod.video_url[:100]}...")
            failed.append(mod.id)
    
    db.commit()
    
    print(f"\n{'='*60}")
    print(f"✅ Migración completada:")
    print(f"   - Módulos migrados: {migrated}")
    print(f"   - Módulos fallidos: {len(failed)}")
    
    if failed:
        print(f"\n⚠️  Módulos que fallaron (revisar manualmente):")
        for mod_id in failed:
            print(f"   - Módulo ID: {mod_id}")
    
    print(f"\n💡 Los endpoints de enrollments y modules ahora regenerarán")
    print(f"   URLs frescas automáticamente cuando se consulten.")
    print(f"{'='*60}")

if __name__ == "__main__":
    migrate_urls()
