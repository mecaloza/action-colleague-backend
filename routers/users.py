import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, hash_password, require_admin
from database import get_db
from models import User
from schemas import OrgChartNode, PermissionsUpdate, RoleUpdate, UserCreate, UserOut, UserProfile, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/org-chart", response_model=List[OrgChartNode])
def org_chart(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    all_users = db.query(User).filter(User.is_active == True).all()
    user_map = {u.id: u for u in all_users}
    children_map: dict[int, list] = {}
    roots = []
    for u in all_users:
        if u.reports_to and u.reports_to in user_map:
            children_map.setdefault(u.reports_to, []).append(u)
        else:
            roots.append(u)

    def build_node(user: User) -> OrgChartNode:
        kids = children_map.get(user.id, [])
        return OrgChartNode(
            id=user.id,
            name=user.name,
            email=user.email,
            position=user.position,
            department=user.department,
            role=user.role,
            children=[build_node(c) for c in kids],
        )

    return [build_node(r) for r in roots]


@router.get("/{user_id}/profile", response_model=UserProfile)
def get_user_profile(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    manager = db.get(User, user.reports_to) if user.reports_to else None
    reports = db.query(User).filter(User.reports_to == user.id, User.is_active == True).all()
    return UserProfile(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        position=user.position,
        department=user.department,
        preferred_language=user.preferred_language,
        hire_date=user.hire_date,
        reports_to=user.reports_to,
        permissions=user.permissions,
        is_active=user.is_active,
        created_at=user.created_at,
        manager=manager,
        direct_reports=reports,
    )


@router.put("/{user_id}/permissions", response_model=UserOut)
def update_permissions(
    user_id: int,
    payload: PermissionsUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.permissions = payload.permissions
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}/role", response_model=UserOut)
def update_role(
    user_id: int,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    if payload.role not in ("admin", "collaborator"):
        raise HTTPException(400, "Role must be 'admin' or 'collaborator'")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = payload.role
    db.commit()
    db.refresh(user)
    return user


@router.get("/", response_model=List[UserOut])
def list_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return db.query(User).offset(skip).limit(limit).all()


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user


@router.post("/", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(400, "Email already registered")
    data = payload.model_dump(exclude={"password", "permissions"})
    user = User(
        **data,
        password_hash=hash_password(payload.password),
        permissions_json=json.dumps(payload.permissions),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    updates = payload.model_dump(exclude_unset=True)

    # Non-admins can only update their own limited fields
    if current_user.role != "admin":
        if user_id != current_user.id:
            raise HTTPException(403, "Cannot update other users")
        allowed = {"name", "password", "preferred_language"}
        disallowed = set(updates.keys()) - allowed
        if disallowed:
            raise HTTPException(403, f"Cannot update fields: {', '.join(disallowed)}")

    if "password" in updates:
        user.password_hash = hash_password(updates.pop("password"))
    if "permissions" in updates:
        user.permissions_json = json.dumps(updates.pop("permissions"))

    for k, v in updates.items():
        setattr(user, k, v)

    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = False
    db.commit()
