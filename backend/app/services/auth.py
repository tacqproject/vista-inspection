"""
Authentication & authorization: password hashing (bcrypt), JWT issuing/decoding
and FastAPI dependencies for pulling the current user and enforcing roles.

The coarse read/write gate lives in a middleware (see app/main.py); these
dependencies are for routes that need the user object or a specific role.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

ALGORITHM = "HS256"

# role → rank; a route requiring rank N accepts any role with rank >= N
ROLE_RANK = {"viewer": 0, "inspector": 1, "admin": 2}


# ─── Passwords ────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ─── Tokens ───────────────────────────────────────────────────────────────────

def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def token_from_request(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


# ─── Dependencies ─────────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve and validate the caller. Rejects unknown/deactivated users."""
    token = token_from_request(request)
    payload = decode_token(token) if token else None
    if not payload:
        raise HTTPException(401, "Nepřihlášeno nebo neplatný token")
    user = await db.get(User, payload.get("sub"))
    if not user or not user.is_active:
        raise HTTPException(401, "Účet neexistuje nebo je deaktivovaný")
    return user


def require_role(min_role: str):
    """Dependency factory: caller must have at least `min_role`."""
    needed = ROLE_RANK[min_role]

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < needed:
            raise HTTPException(403, "Nedostatečná oprávnění")
        return user

    return _dep


require_admin = require_role("admin")
require_editor = require_role("inspector")
