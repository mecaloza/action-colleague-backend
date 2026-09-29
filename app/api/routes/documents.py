from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Document, User
from app.schemas import DocumentCreate, DocumentOut

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_out(doc: Document) -> dict:
    return {
        "id": doc.id,
        "user_id": doc.user_id,
        "type": doc.type,
        "pdf_url": doc.pdf_url,
        "template_data": doc.template_data,
        "generated_at": doc.generated_at,
    }


@router.get("/", response_model=List[DocumentOut])
def list_documents(
    user_id: Optional[int] = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)
):
    q = db.query(Document)
    if user_id is not None:
        q = q.filter(Document.user_id == user_id)
    return [_to_out(d) for d in q.offset(skip).limit(limit).all()]


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return _to_out(doc)


@router.post("/generate", response_model=DocumentOut, status_code=201)
def generate_document(payload: DocumentCreate, db: Session = Depends(get_db)):
    """Generate a labor letter or certificate document for a user."""
    user = db.get(User, payload.user_id)
    if not user:
        raise HTTPException(404, "User not found")

    template_data = {
        "user_name": user.name,
        "email": user.email,
        "position": user.position,
        "department": user.department,
        "hire_date": str(user.hire_date) if user.hire_date else "",
        "generated_at": datetime.utcnow().isoformat(),
        **payload.template_data,
    }

    doc = Document(
        user_id=payload.user_id,
        type=payload.type,
        pdf_url=f"/static/documents/{payload.type}_{user.id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf",
    )
    doc.template_data = template_data
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _to_out(doc)
