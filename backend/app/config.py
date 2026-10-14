from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./vista.db"
    upload_dir: str = "./uploads"
    cors_origins: str = "http://localhost:5173"
    max_upload_size_mb: int = 2048
    meshroom_path: str = "meshroom_batch"  # full path or command name if in PATH
    gltfpack_path: str = "gltfpack"        # for LOD generation; full path or command name

    # Defect-segmentation inference command (runs in its OWN venv — mmcv/ViT-Adapter
    # can't share VISTA's env). VISTA appends --images/--out/--tile/--overlap.
    # e.g. DACL10K_CMD="/path/dacl10k/.venv/bin/python /path/VISTA/ai/dacl10k_infer.py"
    dacl10k_cmd: str = ""

    # Automation: photos uploaded → best-quality model + AI defect inspection.
    default_quality: str = "max"           # quality preset for plain uploads
    auto_defects: bool = True              # detect defects on photos → annotations
    # Large scratch space (OpenMVS depth maps ≈ 50 B/px × 2 per photo, i.e.
    # tens of GB): keep it off the small system disk. Empty = inside uploads/.
    work_dir: str = "/media/epr/ssd/vista-work/scratch"

    # Auth. jwt_secret MUST be set via env in any real deployment; the default
    # is a dev-only fallback (a warning is logged on startup when it's used).
    jwt_secret: str = "vista-dev-insecure-change-me"
    jwt_expire_hours: int = 12
    default_admin_username: str = "admin"
    default_admin_password: str = "admin"

    class Config:
        env_file = ".env"
        extra = "ignore"   # tolerate leftover keys in .env

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
