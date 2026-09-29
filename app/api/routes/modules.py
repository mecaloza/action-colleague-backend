from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.db.models import Module, User
from app.schemas import ModuleCreate, ModuleOut, ModuleUpdate
from app.api.routes.course_wizard import get_fresh_heygen_url

router = APIRouter(prefix="/modules", tags=["modules"])


@router.get("/", response_model=List[ModuleOut])
def list_modules(
    course_id: Optional[int] = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    q = db.query(Module)
    if course_id is not None:
        q = q.filter(Module.course_id == course_id)
    modules = q.order_by(Module.order).offset(skip).limit(limit).all()
    
    # Regenerar URLs de HeyGen para módulos con formato heygen://video/{id}
    result = []
    for module in modules:
        if module.video_url and module.video_url.startswith("heygen://video/"):
            video_id = module.video_url.replace("heygen://video/", "")
            fresh_url = get_fresh_heygen_url(video_id)
            if fresh_url:
                module_dict = {
                    "id": module.id,
                    "course_id": module.course_id,
                    "title": module.title,
                    "order": module.order,
                    "content_text": module.content_text,
                    "video_url": fresh_url,
                    "audio_url": module.audio_url,
                    "generation_status": module.generation_status,
                    "created_at": module.created_at,
                }
                result.append(module_dict)
                continue
        result.append(module)
    
    return result


@router.get("/{module_id}", response_model=ModuleOut)
def get_module(module_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    module = db.get(Module, module_id)
    if not module:
        raise HTTPException(404, "Module not found")
    
    # Regenerar URL de HeyGen si está en formato heygen://video/{id}
    if module.video_url and module.video_url.startswith("heygen://video/"):
        video_id = module.video_url.replace("heygen://video/", "")
        fresh_url = get_fresh_heygen_url(video_id)
        if fresh_url:
            # Crear copia del módulo con URL fresca para respuesta
            # Sin modificar DB
            module_dict = {
                "id": module.id,
                "course_id": module.course_id,
                "title": module.title,
                "order": module.order,
                "content_text": module.content_text,
                "video_url": fresh_url,
                "audio_url": module.audio_url,
                "generation_status": module.generation_status,
                "created_at": module.created_at,
            }
            return module_dict
    
    return module


@router.post("/", response_model=ModuleOut, status_code=201)
def create_module(payload: ModuleCreate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    module = Module(**payload.model_dump())
    db.add(module)
    db.commit()
    db.refresh(module)
    return module


@router.patch("/{module_id}", response_model=ModuleOut)
def update_module(module_id: int, payload: ModuleUpdate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    module = db.get(Module, module_id)
    if not module:
        raise HTTPException(404, "Module not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(module, k, v)
    db.commit()
    db.refresh(module)
    return module


@router.delete("/{module_id}", status_code=204)
def delete_module(module_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    module = db.get(Module, module_id)
    if not module:
        raise HTTPException(404, "Module not found")
    db.delete(module)
    db.commit()
