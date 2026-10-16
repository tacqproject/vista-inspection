import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db, AsyncSessionLocal
from app.ws_manager import ws_manager
from app.routes import structures, inspections, uploads, annotations, measurements, reports, config, ai, auth as auth_routes
from app.services import auth as auth_service

log = logging.getLogger("vista.auth")


# ─── Default seed data ────────────────────────────────────────────────────────

DEFAULT_STRUCTURE_TYPES = [
    {"name": "Budova", "code": "building", "icon": "building-2", "sort_order": 0},
    {"name": "Most", "code": "bridge", "icon": "waypoints", "sort_order": 1},
    {"name": "Věž / stožár", "code": "tower", "icon": "tower-control", "sort_order": 2},
    {"name": "Komín", "code": "chimney", "icon": "flame", "sort_order": 3},
    {"name": "Přehrada / hráz", "code": "dam", "icon": "waves", "sort_order": 4},
    {"name": "Tunel", "code": "tunnel", "icon": "circle-dashed", "sort_order": 5},
    {"name": "Opěrná zeď", "code": "retaining_wall", "icon": "square", "sort_order": 6},
    {"name": "Jiné", "code": "other", "icon": "help-circle", "sort_order": 7},
]

DEFAULT_MATERIALS = [
    {"name": "Beton", "code": "concrete", "color": "#94a3b8", "sort_order": 0},
    {"name": "Železobeton", "code": "reinforced_concrete", "color": "#7c3aed", "sort_order": 1},
    {"name": "Ocel", "code": "steel", "color": "#64748b", "sort_order": 2},
    {"name": "Zdivo", "code": "masonry", "color": "#b45309", "sort_order": 3},
    {"name": "Kámen", "code": "stone", "color": "#78716c", "sort_order": 4},
    {"name": "Dřevo", "code": "wood", "color": "#92400e", "sort_order": 5},
    {"name": "Hliník", "code": "aluminum", "color": "#9ca3af", "sort_order": 6},
    {"name": "Kompozit", "code": "composite", "color": "#0891b2", "sort_order": 7},
    {"name": "Sklo", "code": "glass", "color": "#7dd3fc", "sort_order": 8},
    {"name": "Jiné", "code": "other_material", "color": "#6b7280", "sort_order": 9},
]


async def seed_defaults():
    from sqlalchemy import select
    from app.models import StructureTypeConfig, MaterialConfig

    async with AsyncSessionLocal() as db:
        for item in DEFAULT_STRUCTURE_TYPES:
            existing = await db.execute(
                select(StructureTypeConfig).where(StructureTypeConfig.code == item["code"])
            )
            if not existing.scalar_one_or_none():
                db.add(StructureTypeConfig(**item))

        for item in DEFAULT_MATERIALS:
            existing = await db.execute(
                select(MaterialConfig).where(MaterialConfig.code == item["code"])
            )
            if not existing.scalar_one_or_none():
                db.add(MaterialConfig(**item))

        await db.commit()


async def _fail_orphaned_reconstructions():
    """
    Reconstructions run as in-process background tasks — a server restart
    kills them silently. Mark leftover 'processing' rows as failed so the UI
    doesn't show a phantom progress bar and reprocess isn't blocked.
    """
    from sqlalchemy import select
    from app.models import PhotoSet

    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(PhotoSet).where(PhotoSet.status == "processing"))
        for ps in rows.scalars():
            ps.status = "failed"
            ps.error_message = "Rekonstrukce byla přerušena restartem serveru — spusťte ji znovu."
        await db.commit()


async def seed_admin():
    """Create the initial admin account if there are no users yet."""
    from sqlalchemy import select, func
    from app.models import User

    async with AsyncSessionLocal() as db:
        count = (await db.execute(select(func.count(User.id)))).scalar() or 0
        if count == 0:
            db.add(User(
                username=settings.default_admin_username,
                full_name="Administrátor",
                role="admin",
                hashed_password=auth_service.hash_password(settings.default_admin_password),
                must_change_password=True,
            ))
            await db.commit()
            log.warning(
                "Vytvořen výchozí účet '%s' — změňte počáteční heslo.",
                settings.default_admin_username,
            )
    if settings.jwt_secret == "vista-dev-insecure-change-me":
        log.warning("JWT_SECRET není nastaven — používá se nezabezpečený vývojový klíč. "
                    "Nastavte JWT_SECRET v .env pro produkci.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    await seed_defaults()
    await seed_admin()
    await _fail_orphaned_reconstructions()
    yield


app = FastAPI(
    title="VISTA RAW – samostatná instance",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],   # download filenames (protocol .docx/.pdf)
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# public API endpoints that never require a token
AUTH_EXEMPT = {"/api/auth/login"}


@app.middleware("http")
async def auth_gate(request, call_next):
    """
    Coarse access control for the JSON API:
      • every /api/* call must carry a valid token (except login),
      • writes require at least 'inspector'; 'viewer' is read-only,
      • user management and catalog editing require 'admin'.
    Route-level dependencies enforce the finer-grained rules.
    """
    path = request.url.path
    if not path.startswith("/api/") or request.method == "OPTIONS" or path in AUTH_EXEMPT:
        return await call_next(request)

    # Read-only shared protocol links are public by design (unguessable token).
    if request.method in ("GET", "HEAD") and path.startswith("/api/reports/shared/"):
        return await call_next(request)

    # Photo thumbnails are loaded via <img> tags, which can't send the
    # Authorization header. They serve the same photos already public under
    # /uploads/, so allow GET/HEAD through — no new exposure, read-only.
    if request.method in ("GET", "HEAD") and path.endswith("/thumbnail"):
        return await call_next(request)

    token = auth_service.token_from_request(request)
    payload = auth_service.decode_token(token) if token else None
    if not payload:
        return JSONResponse({"detail": "Nepřihlášeno nebo neplatný token"}, status_code=401)

    role = payload.get("role", "viewer")
    rank = auth_service.ROLE_RANK.get(role, -1)

    if request.method in WRITE_METHODS:
        is_own_account = path.startswith("/api/auth/me")
        needs_admin = path.startswith("/api/auth/users") or path.startswith("/api/config")
        if needs_admin and rank < auth_service.ROLE_RANK["admin"]:
            return JSONResponse({"detail": "Vyžadována práva administrátora"}, status_code=403)
        if not is_own_account and not needs_admin and rank < auth_service.ROLE_RANK["inspector"]:
            return JSONResponse(
                {"detail": "Účet má oprávnění jen pro čtení"}, status_code=403
            )

    return await call_next(request)


@app.middleware("http")
async def uploads_no_cache(request, call_next):
    """
    Model files keep the same names across reconstructions but their content
    (and coordinate frame!) changes — a heuristically-cached stale mesh in the
    browser desyncs clicks from camera poses. Force revalidation (ETag still
    yields cheap 304s for unchanged files).
    """
    response = await call_next(request)
    if request.url.path.startswith("/uploads/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# API routes
app.include_router(auth_routes.router, prefix="/api")
app.include_router(structures.router, prefix="/api")
app.include_router(inspections.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(annotations.router, prefix="/api")
app.include_router(measurements.router, prefix="/api")
app.include_router(reports.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(ai.defects_router, prefix="/api")

# DNG URLs keep the original filename for camera/annotation identity; browsers
# receive a separate display derivative. SfM reads the original file on disk.
@app.get('/uploads/inspections/{inspection_id}/photos/{filename}')
async def display_source_photo(inspection_id: str, filename: str):
    import asyncio
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from app.services.raw_images import source_photo, display_file, is_raw
    try:
        source = source_photo(inspection_id, filename)
        display = await asyncio.to_thread(display_file, source)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, 'Snímek nenalezen')
    return FileResponse(display, media_type='image/jpeg' if is_raw(source) else None)


# Serve uploaded files
uploads_dir = settings.upload_path
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir), follow_symlink=True), name="uploads")  # inspections may live on the data SSD (symlinked)


@app.websocket("/ws/inspections/{inspection_id}")
async def inspection_ws(inspection_id: str, ws: WebSocket):
    await ws_manager.connect(inspection_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(inspection_id, ws)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0-raw", "instance": "raw", "raw_formats": ["dng"]}
