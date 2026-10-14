import zipfile
import shutil
from pathlib import Path
from typing import Optional

from app.config import settings

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def get_inspection_dir(inspection_id: str) -> Path:
    d = settings.upload_path / "inspections" / inspection_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_photos_dir(inspection_id: str) -> Path:
    d = get_inspection_dir(inspection_id) / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_models_dir(inspection_id: str) -> Path:
    d = get_inspection_dir(inspection_id) / "model"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_structure_dir(structure_id: str) -> Path:
    d = settings.upload_path / "structures" / structure_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_uploaded_photos(inspection_id: str, files: list[tuple[str, bytes]]) -> list[Path]:
    photos_dir = get_photos_dir(inspection_id)
    saved = []
    for filename, content in files:
        dest = photos_dir / Path(filename).name
        dest.write_bytes(content)
        saved.append(dest)
    return saved


def extract_zip(inspection_id: str, zip_bytes: bytes) -> list[Path]:
    photos_dir = get_photos_dir(inspection_id)
    tmp_zip = get_inspection_dir(inspection_id) / "upload.zip"
    tmp_zip.write_bytes(zip_bytes)

    extracted = []
    with zipfile.ZipFile(tmp_zip, "r") as zf:
        for member in zf.namelist():
            ext = Path(member).suffix.lower()
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                target = photos_dir / Path(member).name
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(target)

    tmp_zip.unlink(missing_ok=True)
    return extracted


def count_photos(inspection_id: str) -> int:
    photos_dir = get_photos_dir(inspection_id)
    if not photos_dir.exists():
        return 0
    return sum(1 for f in photos_dir.iterdir() if f.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS)


def get_relative_path(abs_path: Path) -> str:
    # Resolve both sides so absolute and relative inputs relativize consistently
    root = settings.upload_path.resolve()
    p = Path(abs_path).resolve()
    try:
        return str(p.relative_to(root))
    except ValueError:
        pass
    # an inspection directory may be a symlink to a big data disk (drone sets
    # with hundreds of photos): map the real path back through the link
    insp = settings.upload_path / "inspections"
    for link in insp.iterdir() if insp.is_dir() else []:
        if link.is_symlink():
            try:
                return str(Path("inspections") / link.name / p.relative_to(link.resolve()))
            except ValueError:
                continue
    return str(abs_path)


def resolve_path(relative: str) -> Optional[Path]:
    p = settings.upload_path / relative
    return p if p.exists() else None
