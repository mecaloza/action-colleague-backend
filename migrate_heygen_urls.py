#!/usr/bin/env python3
"""
Script de migración: Convierte URLs firmadas de HeyGen a formato heygen://video/{id}

Este script busca módulos con video_url que parezcan URLs de S3/HeyGen y intenta
extraer el video_id del API de HeyGen para convertirlas al formato interno.

IMPORTANTE: Ejecutar solo UNA VEZ después de deployar el fix.
"""

import os
import re
import sys
from typing import Optional

# Agregar path para imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SessionLocal
from models import Module


def extract_video_id_from_metadata(module: Module) -> Optional[str]:
    """
    Intenta extraer video_id de metadatos del módulo.
    Podría estar en generation_metadata u otros campos.
    """
    # Aquí podrías buscar en campos adicionales si guardaste el video_id
    # Por ahora retornamos None - manual intervention needed
    return None


def is_heygen_url(url: str) -> bool:
    """Detecta si una URL parece ser de HeyGen/S3."""
    if not url:
        return False
    
    # Patrones comunes de URLs de HeyGen
    patterns = [
        r'https://.*\.amazonaws\.com/.*',
        r'https://.*heygen.*\.com/.*',
        r'https://.*s3.*\.amazonaws\.com/.*',
    ]
    
    return any(re.match(pattern, url) for pattern in patterns)


def main():
    print("🔨 Migración de URLs de HeyGen")
    print("=" * 50)
    
    db = SessionLocal()
    
    try:
        # Buscar módulos con URLs que parezcan de HeyGen
        modules = db.query(Module).filter(
            Module.video_url != "",
            Module.video_url.isnot(None),
        ).all()
        
        heygen_modules = [m for m in modules if is_heygen_url(m.video_url)]
        
        print(f"\n📊 Encontrados {len(heygen_modules)} módulos con URLs de HeyGen")
        
        if not heygen_modules:
            print("✅ No hay módulos para migrar")
            return
        
        print("\n⚠️  ADVERTENCIA:")
        print("Este script necesita video_id para cada módulo.")
        print("Si no guardaste video_id en metadatos, necesitas:")
        print("1. Revisar logs de generación")
        print("2. Contactar soporte de HeyGen")
        print("3. Regenerar videos")
        
        print("\n📋 Módulos encontrados:")
        for mod in heygen_modules:
            video_id = extract_video_id_from_metadata(mod)
            status = "✅ Tiene video_id" if video_id else "❌ Sin video_id"
            print(f"  - Module #{mod.id}: {mod.title[:50]} | {status}")
        
        # Opción para migración manual
        print("\n" + "=" * 50)
        print("MIGRACIÓN MANUAL:")
        print("Si conoces los video_ids, puedes migrar con SQL:")
        print()
        print("UPDATE modules SET video_url = 'heygen://video/{VIDEO_ID}'")
        print("WHERE id = {MODULE_ID};")
        print()
        
        # Auto-migración si tenemos video_ids
        modules_with_id = [(m, extract_video_id_from_metadata(m)) for m in heygen_modules]
        modules_with_id = [(m, vid) for m, vid in modules_with_id if vid]
        
        if modules_with_id:
            print(f"\n✨ Encontrados {len(modules_with_id)} módulos con video_id")
            response = input("¿Migrar automáticamente? (s/N): ")
            
            if response.lower() == 's':
                for module, video_id in modules_with_id:
                    old_url = module.video_url
                    new_url = f"heygen://video/{video_id}"
                    module.video_url = new_url
                    print(f"  ✅ Module #{module.id}: {old_url[:60]}... → {new_url}")
                
                db.commit()
                print(f"\n🎉 Migrados {len(modules_with_id)} módulos exitosamente")
            else:
                print("❌ Migración cancelada")
        else:
            print("\n⚠️  No se puede hacer migración automática sin video_ids")
            print("Ver documentación en HEYGEN_URL_FIX.md para opciones")
    
    except Exception as e:
        print(f"\n❌ Error durante migración: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()
