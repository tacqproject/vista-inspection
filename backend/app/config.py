from pydantic_settings import BaseSettings
from pathlib import Path
from pydantic import model_validator


class Settings(BaseSettings):
    isolated_data_root: str = "./raw-data"
    database_url: str = "sqlite+aiosqlite:///./raw-data/vista.db"
    upload_dir: str = "./raw-data/uploads"
    cors_origins: str = "http://localhost:5174"
    raw_source_root: str = ""
    auto_reconstruct: bool = False
    disk_reserve_gb: float = 10.0
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
    work_dir: str = "./raw-data/scratch"

    @model_validator(mode="after")
    def isolated_paths(self):
        root = Path(self.isolated_data_root).resolve()
        forbidden = (Path('/home/epr/VISTA'), Path('/media/epr/ssd/vista-work'))
        if any(root == p or root.is_relative_to(p) or p.is_relative_to(root) for p in forbidden):
            raise ValueError("RAW instance must use a separate data root")
        prefix = 'sqlite+aiosqlite:///'
        if not self.database_url.startswith(prefix):
            raise ValueError("RAW instance requires its own SQLite database")
        for value in (self.upload_dir, self.work_dir, self.database_url[len(prefix):]):
            if not value or not Path(value).resolve().is_relative_to(root):
                raise ValueError("Database, uploads and scratch must be inside isolated_data_root")
        if self.raw_source_root:
            source = Path(self.raw_source_root).resolve()
            if source.is_relative_to(root) or root.is_relative_to(source):
                raise ValueError("Read-only RAW source and writable data root must be separate")
        return self

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
