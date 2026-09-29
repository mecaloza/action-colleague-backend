"""AI course studio: outlines, storyboards and the materials the AI reads."""

from sqlalchemy.orm import Session

from app.db.models import Course, MediaAsset, Module
from app.services.ai.designer import CourseOutline, OutlineModule
from app.services.course_views import ACTIVE_GENERATION
from app.services.progress import ordered_modules

MATERIAL_KINDS = ("document", "deck")


def _asset_text(asset: MediaAsset) -> str:
    """The text extracted from an uploaded document (stored by the media.process job)."""
    return (asset.meta or {}).get("text", "")


def materials_text(db: Session, course_id: int) -> str:
    """Text of every processed document/deck uploaded for the course, with its file name."""
    assets = (
        db.query(MediaAsset)
        .filter(MediaAsset.course_id == course_id, MediaAsset.kind.in_(MATERIAL_KINDS), MediaAsset.status == "ready")
        .order_by(MediaAsset.created_at, MediaAsset.id)
        .all()
    )
    return "\n\n".join(f"### {asset.original_filename or 'Documento'}\n{_asset_text(asset)}" for asset in assets)


def stored_outline(course: Course) -> CourseOutline | None:
    """The outline proposed for the course (kept in its settings), if there is one."""
    raw = (course.settings or {}).get("outline")
    return CourseOutline.model_validate(raw) if raw else None


def module_outline(module: Module) -> OutlineModule:
    """The outline entry a module was created from (with its current title and description), or one derived from them."""
    raw = (module.storyboard or {}).get("outline")
    if raw:
        entry = OutlineModule.model_validate(raw)
        # The admin may have renamed the module in the editor since the outline was approved.
        return entry.model_copy(update={"title": module.title, "summary": module.description or entry.summary})
    return OutlineModule(
        title=module.title,
        summary=module.description or "",
        objectives=[],
        key_points=[],
        estimated_minutes=5,
        include_quiz=True,
    )


def course_outline(course: Course) -> CourseOutline:
    """The approved outline, kept in sync with the course's current modules and order."""
    outline = stored_outline(course)
    return CourseOutline(
        title=course.title,
        description=course.description or "",
        audience=(course.settings or {}).get("audience") or (outline.audience if outline else ""),
        objectives=outline.objectives if outline else [],
        modules=[module_outline(module) for module in ordered_modules(course)],
    )


def scenes_of(module: Module) -> list[dict]:
    return list((module.storyboard or {}).get("scenes") or [])


def is_untouched_ai_module(module: Module) -> bool:
    """Created from an outline and not drafted (nor being drafted), produced or edited yet: safe to replace."""
    return (
        module.source == "ai"
        and module.generation_status not in ACTIVE_GENERATION
        and not scenes_of(module)
        and not module.video_asset_id
        and not module.video_url
        and not (module.content_text or "").strip()
        and module.evaluation is None
    )


def narration_text(module: Module) -> str:
    return "\n\n".join(scene.get("narration", "") for scene in scenes_of(module))


def module_content_for_quiz(db: Session, module: Module) -> str:
    """Everything a learner studies in the module: reading, narration and the attached document."""
    parts = [module.content_text or "", narration_text(module)]
    document = db.get(MediaAsset, module.document_asset_id) if module.document_asset_id else None
    if document:
        parts.append(_asset_text(document))
    return "\n\n".join(part for part in parts if part and part.strip())
