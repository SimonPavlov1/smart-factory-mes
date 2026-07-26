from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth import User
from app.models.production import WorkflowTask
from app.schemas.auth import LoginRequest, MeResponse, TokenResponse, UserCreate, UserOut, UserUpdate, VALID_ROLES
from app.services.auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    permissions_for_roles,
    require_roles,
    user_has_role,
    user_roles,
    verify_password,
)

router = APIRouter(tags=["Авторизация"])


def _validate_role(role: str):
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Неизвестная роль: {role}")


def _full_name_from_parts(user: User) -> str | None:
    parts = [user.last_name, user.first_name, user.middle_name]
    value = " ".join(part.strip() for part in parts if part and part.strip())
    return value or user.full_name


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _phone_username(phone: str) -> str:
    cleaned = "".join(ch for ch in phone if ch.isdigit())
    return f"phone_{cleaned}" if cleaned else phone


def _normalize_phone(value: str | None) -> str | None:
    value = _clean(value)
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return value


def _phone_lookup_values(value: str | None) -> list[str]:
    raw = _clean(value)
    normalized = _normalize_phone(value)
    digits = "".join(ch for ch in raw or "" if ch.isdigit())
    values = [normalized, raw]
    if len(digits) == 10:
        values.extend([digits, f"8{digits}", f"7{digits}", f"+7{digits}"])
    elif len(digits) == 11:
        values.extend([digits, f"+{digits}"])
        if digits.startswith("7"):
            values.append(f"8{digits[1:]}")
        if digits.startswith("8"):
            values.append(f"+7{digits[1:]}")
    return list(dict.fromkeys(value for value in values if value))


def _validate_roles(roles: list[str] | None, fallback: str | None = None) -> list[str]:
    cleaned = []
    for role in roles or []:
        if role and role not in cleaned:
            _validate_role(role)
            cleaned.append(role)
    if not cleaned and fallback:
        _validate_role(fallback)
        cleaned.append(fallback)
    return cleaned


def _user_out(user: User) -> UserOut:
    roles = user_roles(user)
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=_full_name_from_parts(user),
        last_name=user.last_name,
        first_name=user.first_name,
        middle_name=user.middle_name,
        phone=user.phone,
        role=user.role,
        roles=roles,
        is_active=user.is_active,
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    identity = _normalize_phone(payload.phone) or _clean(payload.username)
    if not identity:
        raise HTTPException(status_code=422, detail="Укажите телефон")

    user = db.query(User).filter(User.phone.in_(_phone_lookup_values(identity))).first()
    if not user:
        user = db.query(User).filter(User.username == identity).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный телефон или пароль")
    return TokenResponse(access_token=create_access_token(user))


@router.get("/auth/me", response_model=MeResponse)
def get_me(user: User = Depends(get_current_user)):
    roles = user_roles(user)
    return MeResponse(
        id=user.id,
        username=user.username,
        full_name=_full_name_from_parts(user),
        last_name=user.last_name,
        first_name=user.first_name,
        middle_name=user.middle_name,
        phone=user.phone,
        role=user.role,
        roles=roles,
        is_active=user.is_active,
        permissions=permissions_for_roles(roles),
    )


@router.get("/admin/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    return [_user_out(user) for user in db.query(User).order_by(User.username).all()]


@router.get("/users", response_model=list[UserOut])
def list_active_users(
    role: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    query = db.query(User).filter(User.is_active == True)
    if role:
        _validate_role(role)
        users = [
            user for user in query.order_by(User.full_name, User.username).all()
            if role in user_roles(user) or user_has_role(user, "admin", "manager", "production_manager")
        ]
        return [_user_out(user) for user in users]
    return [_user_out(user) for user in query.order_by(User.full_name, User.username).all()]


@router.post("/admin/users", response_model=UserOut)
def create_user(payload: UserCreate, db: Session = Depends(get_db), _: User = Depends(require_roles("admin"))):
    roles = _validate_roles(payload.roles, payload.role)
    primary_role = roles[0]
    phone = _normalize_phone(payload.phone)
    if not phone:
        raise HTTPException(status_code=422, detail="Телефон обязателен")

    username = _clean(payload.username) or _phone_username(phone)
    existing = db.query(User).filter((User.username == username) | (User.phone == phone)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Пользователь с таким телефоном уже существует")

    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        full_name=_clean(payload.full_name),
        last_name=_clean(payload.last_name),
        first_name=_clean(payload.first_name),
        middle_name=_clean(payload.middle_name),
        phone=phone,
        role=primary_role,
        roles=roles,
        is_active=True,
    )
    user.full_name = _full_name_from_parts(user)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.put("/admin/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if payload.roles is not None or payload.role is not None:
        roles = _validate_roles(payload.roles, payload.role or user.role)
        user.roles = roles
        user.role = roles[0]
    if payload.full_name is not None:
        user.full_name = _clean(payload.full_name)
    if payload.last_name is not None:
        user.last_name = _clean(payload.last_name)
    if payload.first_name is not None:
        user.first_name = _clean(payload.first_name)
    if payload.middle_name is not None:
        user.middle_name = _clean(payload.middle_name)
    if payload.phone is not None:
        phone = _normalize_phone(payload.phone)
        if phone:
            existing = db.query(User).filter(User.phone == phone, User.id != user.id).first()
            if existing:
                raise HTTPException(status_code=400, detail="Пользователь с таким телефоном уже существует")
        user.phone = phone
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        user.password_hash = hash_password(payload.password)

    user.full_name = _full_name_from_parts(user)

    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.delete("/admin/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить текущую учетную запись")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    db.query(WorkflowTask).filter(
        WorkflowTask.assigned_user_id == user_id,
        WorkflowTask.status != "done",
    ).update(
        {
            WorkflowTask.assigned_user_id: None,
            WorkflowTask.started_at: None,
            WorkflowTask.status: "assigned",
        },
        synchronize_session=False,
    )
    db.query(WorkflowTask).filter(WorkflowTask.assigned_user_id == user_id).update(
        {WorkflowTask.assigned_user_id: None, WorkflowTask.started_at: None},
        synchronize_session=False,
    )
    db.delete(user)
    db.commit()
    return {"status": "success"}
