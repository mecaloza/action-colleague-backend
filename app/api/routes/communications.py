import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from openai import OpenAI
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.db.models import Communication, User
from app.schemas import (
    CommunicationCreate,
    CommunicationResponse,
    ImageGenerateRequest,
    ImageGenerateResponse,
)

router = APIRouter(prefix="/communications", tags=["communications"])


@router.post("/", response_model=CommunicationResponse, status_code=201)
def create_communication(
    payload: CommunicationCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    comm = Communication(**payload.model_dump())
    db.add(comm)
    db.commit()
    db.refresh(comm)
    return comm


@router.get("/", response_model=List[CommunicationResponse])
def list_communications(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return (
        db.query(Communication)
        .order_by(Communication.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{communication_id}", response_model=CommunicationResponse)
def get_communication(
    communication_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    comm = db.get(Communication, communication_id)
    if not comm:
        raise HTTPException(404, "Communication not found")
    return comm


@router.delete("/{communication_id}", status_code=204)
def delete_communication(
    communication_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    comm = db.get(Communication, communication_id)
    if not comm:
        raise HTTPException(404, "Communication not found")
    db.delete(comm)
    db.commit()


@router.post("/generate-image", response_model=ImageGenerateResponse)
def generate_image(
    payload: ImageGenerateRequest,
    _admin: User = Depends(require_admin),
):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY not configured")

    client = OpenAI(api_key=api_key)
    response = client.images.generate(
        model="dall-e-3",
        prompt=payload.prompt,
        size="1024x1024",
        n=1,
    )
    image_url = response.data[0].url
    return ImageGenerateResponse(image_url=image_url, prompt_used=payload.prompt)
