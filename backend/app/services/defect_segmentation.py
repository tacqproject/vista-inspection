"""
AI defect segmentation over all photos of an inspection.

The heavy model (dacl10k ViT-Adapter, mmcv CUDA ops) can't live in VISTA's venv,
so it runs as an EXTERNAL command in its own environment — exactly like the
Meshroom Docker wrapper. VISTA only orchestrates: it hands the inferer an images
directory and reads back per-image class-coverage + overlay images.

Inference-command contract
──────────────────────────
    <DACL10K_CMD> --images <dir> --out <dir> --tile 1024 --overlap 128 [--device cuda]

The command must write into <out>:
    results.json  →  {"classes": [...], "images": [
                        {"filename": "...", "overlay": "overlay_x.jpg",
                         "coverage": {"Crack": 0.013, ...}}, ...]}
    overlay_*.jpg →  colour overlay per image (referenced by results.json)

Optionally it may print `PROGRESS <pct> <message>` lines to stdout for live
progress. See ai/dacl10k_infer.py (real model) and ai/mock_infer.py (testing).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.config import settings
from app.services.storage import get_inspection_dir, get_photos_dir, get_relative_path

# dacl10k's 19 classes (14 damage + 5 objects/components).
CLASSES = [
    "Crack", "ACrack", "Wetspot", "Efflorescence", "Rust", "Rockpocket",
    "Hollowareas", "Cavity", "Spalling", "Graffiti", "Weathering",
    "Restformwork", "ExposedRebars", "Bearing", "EJoint", "Drainage",
    "PEquipment", "JTape", "WConccor",
]

# The subset that is actual structural damage (the rest are components/markings).
DAMAGE_CLASSES = [
    "Crack", "ACrack", "Wetspot", "Efflorescence", "Rust", "Rockpocket",
    "Hollowareas", "Cavity", "Spalling", "Weathering", "ExposedRebars", "WConccor",
]

CLASS_LABELS = {
    "Crack": "Trhlina", "ACrack": "Mapová trhlina", "Wetspot": "Zavlhlé místo",
    "Efflorescence": "Výkvět solí", "Rust": "Koroze / rez", "Rockpocket": "Štěrkové hnízdo",
    "Hollowareas": "Duté místo", "Cavity": "Kaverna", "Spalling": "Odprýskání betonu",
    "Graffiti": "Graffiti", "Weathering": "Povětrnostní degradace",
    "Restformwork": "Zbytky bednění", "ExposedRebars": "Obnažená výztuž",
    "Bearing": "Ložisko", "EJoint": "Dilatační spára", "Drainage": "Odvodnění",
    "PEquipment": "Vybavení", "JTape": "Těsnicí páska", "WConccor": "Koroze betonu (voda)",
}

ProgressFn = Callable[[int, str], Awaitable[None]]


def _out_dir(inspection_id: str) -> Path:
    return get_inspection_dir(inspection_id) / "ai_segmentation"


def _status_path(inspection_id: str) -> Path:
    return _out_dir(inspection_id) / "status.json"


def _results_path(inspection_id: str) -> Path:
    return _out_dir(inspection_id) / "results.json"


def check() -> dict:
    """Availability of the configured inference command (for /config + UI)."""
    cmd = settings.dacl10k_cmd.strip()
    if not cmd:
        return {
            "available": False,
            "hint": "Nastavte DACL10K_CMD='/cesta/k/.venv/bin/python /cesta/VISTA/ai/dacl10k_infer.py' v .env",
        }
    exe = shlex.split(cmd)[0]
    ok = Path(exe).is_file() or _which(exe) is not None
    return {"available": ok, "cmd": cmd,
            "hint": None if ok else f"Příkaz '{exe}' nebyl nalezen"}


def _which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


def _write_status(inspection_id: str, **fields) -> None:
    p = _status_path(inspection_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated": int(time.time()), **fields}
    p.write_text(json.dumps(data))


def load_results(inspection_id: str) -> dict:
    """Merged status + parsed results (overlay paths → served /uploads URLs)."""
    status = {"status": "none"}
    sp = _status_path(inspection_id)
    if sp.exists():
        try:
            status = json.loads(sp.read_text())
        except Exception:
            pass

    out = {**status}
    rp = _results_path(inspection_id)
    if rp.exists():
        try:
            res = json.loads(rp.read_text())
            od = _out_dir(inspection_id)
            for img in res.get("images", []):
                ov = img.get("overlay")
                if ov:
                    img["overlay_url"] = f"/uploads/{get_relative_path(od / ov)}"
            out["classes"] = res.get("classes", CLASSES)
            out["images"] = res.get("images", [])
        except Exception:
            pass
    return out


async def run_segmentation(
    inspection_id: str,
    on_progress: Optional[ProgressFn] = None,
    tile: int = 1024,
    overlap: int = 128,
) -> dict:
    """Run the external inferer over every photo of the inspection."""
    photos_dir = get_photos_dir(inspection_id)
    out_dir = _out_dir(inspection_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    async def report(pct: int, msg: str):
        _write_status(inspection_id, status="processing", progress=pct, message=msg)
        if on_progress:
            await on_progress(pct, msg)

    cmd = settings.dacl10k_cmd.strip()
    if not cmd:
        _write_status(inspection_id, status="failed",
                      error="AI model není nakonfigurován (DACL10K_CMD).")
        raise RuntimeError("DACL10K_CMD není nastaven")

    n_photos = sum(1 for f in photos_dir.glob("*") if f.suffix.lower() in
                   (".jpg", ".jpeg", ".png", ".tif", ".tiff"))
    if n_photos == 0:
        _write_status(inspection_id, status="failed", error="Žádné snímky k analýze.")
        raise RuntimeError("Žádné snímky")

    await report(2, f"Spouštím AI segmentaci na {n_photos} snímcích…")

    args = shlex.split(cmd) + [
        "--images", str(photos_dir.resolve()),
        "--out", str(out_dir.resolve()),
        "--tile", str(tile), "--overlap", str(overlap),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    log = out_dir / "infer.log"
    assert proc.stdout is not None
    with open(log, "w", encoding="utf-8") as lf:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            lf.write(line)
            lf.flush()
            line = line.strip()
            if line.startswith("PROGRESS"):
                parts = line.split(maxsplit=2)
                try:
                    pct = int(parts[1])
                    await report(pct, parts[2] if len(parts) > 2 else "Zpracovávám…")
                except (ValueError, IndexError):
                    pass

    rc = await proc.wait()
    if rc != 0 or not _results_path(inspection_id).exists():
        _write_status(inspection_id, status="failed",
                      error=f"Inference selhala (kód {rc}). Detaily: {log}")
        raise RuntimeError(f"AI inference selhala (kód {rc})")

    # Summaries for quick display: total images with any damage + top classes.
    res = json.loads(_results_path(inspection_id).read_text())
    imgs = res.get("images", [])
    with_damage = sum(
        1 for im in imgs
        if any(im.get("coverage", {}).get(c, 0) > 0.001 for c in DAMAGE_CLASSES)
    )
    _write_status(inspection_id, status="completed", progress=100,
                  message="Hotovo", n_images=len(imgs), n_with_damage=with_damage)
    return load_results(inspection_id)
