"""Admin: rankings. Who earned the most points today, this week, this month or ever; over every course or in a path."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.db.models import LearningPath
from app.schemas.ranking import Period, Ranking
from app.services import ranking

router = APIRouter(prefix="/ranking", tags=["ranking"], dependencies=[Depends(require_admin)])


@router.get("", response_model=Ranking)
def get_ranking(
    period: Period = "today",
    path_id: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """The top `limit` of the period (in the platform's time zone), and how many people are ranked in it. With a path
    (in any status), its people and what they earned in its courses and for completing it."""
    path = None
    if path_id is not None:
        path = db.get(LearningPath, path_id)
        if path is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta no encontrada")
    return ranking.admin_ranking(db, period, path, limit)
