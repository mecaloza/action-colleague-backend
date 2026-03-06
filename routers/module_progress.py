from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import ModuleProgress
from schemas import ModuleProgressCreate, ModuleProgressOut, ModuleProgressUpdate

router = APIRouter(prefix="/module-progress", tags=["module_progress"])


@router.get("/", response_model=List[ModuleProgressOut])
def list_module_progress(
    enrollment_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(ModuleProgress)
    if enrollment_id is not None:
        q = q.filter(ModuleProgress.enrollment_id == enrollment_id)
    return q.offset(skip).limit(limit).all()


@router.get("/{progress_id}", response_model=ModuleProgressOut)
def get_module_progress(progress_id: int, db: Session = Depends(get_db)):
    mp = db.get(ModuleProgress, progress_id)
    if not mp:
        raise HTTPException(404, "Module progress not found")
    return mp


@router.post("/", response_model=ModuleProgressOut, status_code=201)
def create_module_progress(payload: ModuleProgressCreate, db: Session = Depends(get_db)):
    mp = ModuleProgress(**payload.model_dump())
    if mp.completed:
        mp.completed_at = datetime.utcnow()
    db.add(mp)
    db.commit()
    db.refresh(mp)
    return mp


@router.patch("/{progress_id}", response_model=ModuleProgressOut)
def update_module_progress(
    progress_id: int, payload: ModuleProgressUpdate, db: Session = Depends(get_db)
):
    mp = db.get(ModuleProgress, progress_id)
    if not mp:
        raise HTTPException(404, "Module progress not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(mp, k, v)
    if data.get("completed") and not mp.completed_at:
        mp.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(mp)
    return mp
