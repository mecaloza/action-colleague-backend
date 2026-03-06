from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Module, User
from schemas import ModuleCreate, ModuleOut, ModuleUpdate

router = APIRouter(prefix="/modules", tags=["modules"])


@router.get("/", response_model=List[ModuleOut])
def list_modules(
    course_id: Optional[int] = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    q = db.query(Module)
    if course_id is not None:
        q = q.filter(Module.course_id == course_id)
    return q.order_by(Module.order).offset(skip).limit(limit).all()


@router.get("/{module_id}", response_model=ModuleOut)
def get_module(module_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    module = db.get(Module, module_id)
    if not module:
        raise HTTPException(404, "Module not found")
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
