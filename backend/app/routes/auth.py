"""
Auth & user management.

  POST   /auth/login              — obtain a JWT
  GET    /auth/me                 — current user
  POST   /auth/me/password        — change own password
  GET    /auth/users              — list users            (admin)
  POST   /auth/users              — create user           (admin)
  PATCH  /auth/users/{id}         — edit role/name/active (admin)
  POST   /auth/users/{id}/password— reset a user's password (admin)
  DELETE /auth/users/{id}         — delete user           (admin)
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import (
    LoginIn, TokenOut, UserOut, UserCreate, UserUpdate, PasswordChange,
)
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(User).where(User.username == body.username.strip()))
    user = r.scalar_one_or_none()
    if not user or not auth.verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Nesprávné jméno nebo heslo")
    if not user.is_active:
        raise HTTPException(403, "Účet je deaktivovaný")
    user.last_login = datetime.utcnow()
    await db.commit()
    return TokenOut(access_token=auth.create_token(user), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(auth.get_current_user)):
    return user


@router.post("/me/password", status_code=204)
async def change_own_password(
    body: PasswordChange,
    user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.current_password or not auth.verify_password(
        body.current_password, user.hashed_password
    ):
        raise HTTPException(400, "Současné heslo není správné")
    user.hashed_password = auth.hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()


# ─── Admin: user management ───────────────────────────────────────────────────

@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: User = Depends(auth.require_admin),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(User).order_by(User.created_at))
    return list(r.scalars().all())


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    _: User = Depends(auth.require_admin),
    db: AsyncSession = Depends(get_db),
):
    exists = await db.execute(select(User).where(User.username == body.username))
    if exists.scalar_one_or_none():
        raise HTTPException(409, "Uživatel s tímto jménem už existuje")
    user = User(
        username=body.username,
        full_name=body.full_name,
        role=body.role,
        hashed_password=auth.hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserUpdate,
    admin: User = Depends(auth.require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Uživatel nenalezen")
    # Guard: don't let an admin lock everyone out by demoting/disabling the
    # last remaining active admin (often themselves).
    if user.role == "admin" and (body.role not in (None, "admin") or body.is_active is False):
        active_admins = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )
        if (active_admins.scalar() or 0) <= 1:
            raise HTTPException(400, "Nelze odebrat práva poslednímu aktivnímu administrátorovi")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{user_id}/password", status_code=204)
async def reset_user_password(
    user_id: str,
    body: PasswordChange,
    _: User = Depends(auth.require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Uživatel nenalezen")
    user.hashed_password = auth.hash_password(body.new_password)
    user.must_change_password = True  # prompt them to set their own on next login
    await db.commit()


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(auth.require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Uživatel nenalezen")
    if user.id == admin.id:
        raise HTTPException(400, "Nelze smazat vlastní účet")
    if user.role == "admin":
        active_admins = await db.execute(
            select(func.count(User.id)).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )
        if (active_admins.scalar() or 0) <= 1:
            raise HTTPException(400, "Nelze smazat posledního administrátora")
    await db.delete(user)
    await db.commit()
