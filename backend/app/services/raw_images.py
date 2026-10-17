"""DNG originals stay in photos/. Display/AI derivatives never feed SfM.

Decode the active sensor area without auto rotation/cropping, matching
AliceVision's DNG coordinates. OpenMVS consumes full-size lossless RGB8 PNG;
its image pipeline is RGB8, so this is not a 16-bit end-to-end workflow.
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app.config import settings

RAW_EXTENSIONS = {".dng"}
_decode_slots = threading.BoundedSemaphore(2)
_cache_lock = threading.Lock()


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def decode_rgb(path: str | Path, *, half_size: bool = False):
    import rawpy
    with _decode_slots, rawpy.imread(str(path)) as raw:
        return raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                               output_color=rawpy.ColorSpace.sRGB, output_bps=8,
                               user_flip=0, half_size=half_size)


def source_photo(inspection_id: str, filename: str) -> Path:
    from .storage import get_photos_dir, ALLOWED_IMAGE_EXTENSIONS
    if not filename or Path(filename).name != filename or filename in {'.', '..'}:
        raise ValueError("Invalid photo name")
    base = get_photos_dir(inspection_id).resolve()
    source = base / filename
    target = source.resolve()
    roots = [base, *settings.raw_source_paths]
    if not any(target.is_relative_to(root) for root in roots):
        raise ValueError("Photo escapes permitted source directories")
    if not source.is_file() or source.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        raise FileNotFoundError(filename)
    return source


def display_file(source: str | Path, size: int | None = None) -> Path:
    source = Path(source)
    if size is None and not is_raw(source):
        return source
    st = source.stat()
    key = hashlib.sha256(f"v1:{source.absolute()}:{st.st_size}:{st.st_mtime_ns}:{size}".encode()).hexdigest()
    cache = Path(settings.work_dir) / 'raw-previews'
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f'{key}.jpg'
    if dest.exists():
        return dest
    # Serialize cache misses: a gallery must not launch hundreds of decoders.
    with _cache_lock:
        if dest.exists():
            return dest
        import shutil
        if shutil.disk_usage(cache).free < settings.disk_reserve_gb * 1e9 + 100_000_000:
            raise RuntimeError('Nedostatek místa pro RAW náhled; uvolněte místo na SSD.')
        if is_raw(source):
            img = Image.fromarray(decode_rgb(source, half_size=bool(size)))
        else:
            with Image.open(source) as original:
                img = ImageOps.exif_transpose(original).convert('RGB')
        if size:
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
        temp = dest.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        try:
            img.save(temp, format='JPEG', quality=85 if size else 98, subsampling=0)
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
            img.close()
    return dest


def read_bgr(source: str | Path, flags: int | None = None):
    import cv2
    flags = cv2.IMREAD_COLOR if flags is None else flags
    if not is_raw(source):
        return cv2.imread(str(source), flags)
    rgb = decode_rgb(source)
    gray = flags in (cv2.IMREAD_GRAYSCALE, cv2.IMREAD_REDUCED_GRAYSCALE_2,
                     cv2.IMREAD_REDUCED_GRAYSCALE_4, cv2.IMREAD_REDUCED_GRAYSCALE_8)
    result = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY if gray else cv2.COLOR_RGB2BGR)
    factors = {cv2.IMREAD_REDUCED_COLOR_2: 2, cv2.IMREAD_REDUCED_GRAYSCALE_2: 2,
               cv2.IMREAD_REDUCED_COLOR_4: 4, cv2.IMREAD_REDUCED_GRAYSCALE_4: 4,
               cv2.IMREAD_REDUCED_COLOR_8: 8, cv2.IMREAD_REDUCED_GRAYSCALE_8: 8}
    factor = factors.get(flags, 1)
    if factor != 1:
        result = cv2.resize(result, (result.shape[1] // factor, result.shape[0] // factor),
                            interpolation=cv2.INTER_AREA)
    return result


def ai_inputs(inspection_id: str) -> tuple[Path, dict[str, str]]:
    from .storage import get_photos_dir, ALLOWED_IMAGE_EXTENSIONS
    import shutil
    sources = sorted(p for p in get_photos_dir(inspection_id).iterdir()
                     if p.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS)
    if not any(is_raw(p) for p in sources):
        return get_photos_dir(inspection_id).resolve(), {}
    target = Path(settings.work_dir) / inspection_id / 'ai-images'
    target.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for source in sources:
        name = source.name + '.jpg' if is_raw(source) else source.name
        mapping[name] = source.name
        derivative = display_file(source)
        dest = target / name
        if not dest.exists() or dest.stat().st_mtime_ns < source.stat().st_mtime_ns:
            shutil.copyfile(derivative, dest)
    for old in target.iterdir():
        if old.name not in mapping:
            old.unlink()
    return target.resolve(), mapping
