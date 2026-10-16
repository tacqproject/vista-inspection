import zipfile
import io
import shutil
import os
import tempfile
from pathlib import Path
from typing import Optional

from app.config import settings

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".dng"}


def get_inspection_dir(inspection_id: str) -> Path:
    if not inspection_id or Path(inspection_id).name != inspection_id or inspection_id in {'.', '..'}:
        raise ValueError("Invalid inspection id")
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
        if dest.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("Unsupported image format")
        if shutil.disk_usage(photos_dir).free < settings.disk_reserve_gb * 1e9 + len(content):
            raise ValueError("Nedostatek volného místa pro nahrání fotografie")
        # Replace the directory entry, never follow a link to a shared original.
        fd, name = tempfile.mkstemp(dir=photos_dir)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as tmp:
                tmp.write(content)
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
        saved.append(dest)
    return saved


def extract_zip(inspection_id: str, zip_bytes: bytes) -> list[Path]:
    photos_dir = get_photos_dir(inspection_id)
    extracted = []
    # The request already holds the archive: do not consume SSD reserve with
    # a second copy. Check uncompressed size before allocating/extracting it.
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for member in zf.infolist():
            if member.is_dir() or Path(member.filename).suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            if shutil.disk_usage(photos_dir).free < settings.disk_reserve_gb * 1e9 + member.file_size:
                raise ValueError("Nedostatek volného místa pro rozbalení fotografie")
            extracted.extend(save_uploaded_photos(inspection_id, [(Path(member.filename).name, zf.read(member))]))
    return extracted


def count_photos(inspection_id: str) -> int:
    photos_dir = get_photos_dir(inspection_id)
    if not photos_dir.exists():
        return 0
    return sum(1 for f in photos_dir.iterdir() if f.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS)


def get_relative_path(abs_path: Path) -> str:
    try:
        return str(Path(abs_path).absolute().relative_to(settings.upload_path.absolute()))
    except ValueError:
        pass
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
