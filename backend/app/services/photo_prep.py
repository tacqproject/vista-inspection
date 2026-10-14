"""
Photo preprocessing before reconstruction (originals stay untouched — the app,
reports and defect detection keep using them; only the 3D reconstruction and
its textures use the prepared copies, same file names).

1. Exposure balance from metadata. Each photo's exposure (t · ISO / N²) from
   EXIF → all photos are brought to the SAME exposure in linear light, so a
   wall has the same brightness in every photo regardless of what the camera's
   auto-exposure did (no blotchy texture, better matching).
2. One global tone curve for the whole set (not per photo: per-photo histogram
   equalisation would make the same surface look different in each photo):
   shadows lifted, highlights rolled off — harsh sun under a bridge leaves deep
   shadows and bright concrete in the same frame.
3. Culling: motion-blurred photos (much less sharp than their neighbours in
   capture order) and near-duplicates (practically the same view as the last
   kept photo — hovering drone, burst) are left out. Conservative: never more
   than MAX_DROP of the set, never on small sets.

EXIF (focal length, GPS, camera model) is copied to the prepared JPEGs — the
reconstruction needs it.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
MAX_DROP = 0.35          # never drop more than 35 % of the photos
MIN_SET = 30             # no culling below this many photos
DUP_NCC = 0.95           # thumbnail correlation above this = same view
DUP_GPS_M = 0.6          # ... and (when GPS is known) closer than this
BLUR_REL = 0.5           # edge sharpness below 50 % of the local median = clearly blurred (low-texture soffit shots next to textured ones score ~0.6–0.7, keep them)


def _rational(v) -> float | None:
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        try:
            return float(v[0]) / float(v[1])
        except Exception:  # noqa: BLE001
            return None


def _meta(path: Path) -> dict:
    from PIL import Image
    im = Image.open(path)
    ex = im.getexif()
    sub = ex.get_ifd(0x8769)
    t = _rational(sub.get(0x829A))            # ExposureTime
    n = _rational(sub.get(0x829D))            # FNumber
    iso = sub.get(0x8827)                     # ISOSpeedRatings
    iso = float(iso[0] if isinstance(iso, (tuple, list)) else iso) if iso else None
    when = sub.get(0x9003) or ex.get(0x0132)  # DateTimeOriginal / DateTime
    gps = None
    g = ex.get_ifd(0x8825)
    if g and 2 in g and 4 in g:
        dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        lat, lon = dms(g[2]), dms(g[4])
        lat = -lat if g.get(1) == "S" else lat
        lon = -lon if g.get(3) == "W" else lon
        gps = (lat, lon, float(g[6]) if 6 in g else 0.0)
    k = t * iso / (n * n) if (t and iso and n) else None
    return {"name": path.name, "exposure": k, "time": str(when or ""), "gps": gps,
            "exif": im.info.get("exif"), "xmp": im.info.get("xmp")}


def _analyse(path: Path) -> dict:
    """Metadata + sharpness + small normalised thumbnail for duplicate tests."""
    import cv2
    m = _meta(path)
    img = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_2)
    if img is None:
        return {**m, "ok": False}
    # sharpness independent of how much texture the view has: high-frequency
    # response relative to gradient strength, on the strongest edges only
    # (a smooth soffit + sky is sharp, just has few edges)
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0); gy = cv2.Sobel(img, cv2.CV_32F, 0, 1)
    g = np.hypot(gx, gy)
    lap = np.abs(cv2.Laplacian(img, cv2.CV_32F))
    strong = g > np.percentile(g, 95)
    m["sharp"] = float(lap[strong].mean() / (g[strong].mean() + 1e-6)) if strong.any() else 0.0
    v = img.astype(np.float32) / 255.0
    m["p99_lin"] = float(np.percentile(np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4), 99.5))
    th = cv2.resize(img, (96, 72), interpolation=cv2.INTER_AREA).astype(np.float32)
    th = (th - th.mean()) / (th.std() + 1e-6)
    m["thumb"] = th
    m["lum"] = float(np.median(img))
    m["ok"] = True
    return m


def _enu(gps_list):
    g = np.array(gps_list, float)
    lat0, lon0 = g[:, 0].mean(), g[:, 1].mean()
    R = 6378137.0
    return np.stack([np.radians(g[:, 1] - lon0) * R * math.cos(math.radians(lat0)),
                     np.radians(g[:, 0] - lat0) * R, g[:, 2] - g[:, 2].mean()], 1)


def select(infos: list[dict]) -> tuple[list[dict], list[dict]]:
    """→ (kept, dropped with a reason). Capture order = EXIF time, then name."""
    infos = sorted([i for i in infos if i["ok"]], key=lambda i: (i["time"], i["name"]))
    n = len(infos)
    if n < MIN_SET:
        return infos, []
    drop: dict[str, str] = {}
    sharp = np.array([i["sharp"] for i in infos])
    for k, i in enumerate(infos):
        lo, hi = max(0, k - 6), min(n, k + 7)
        if i["sharp"] < BLUR_REL * np.median(sharp[lo:hi]):
            drop[i["name"]] = "rozmazaná"
    has_gps = all(i["gps"] for i in infos)
    enu = _enu([i["gps"] for i in infos]) if has_gps else None
    last = None
    for k, i in enumerate(infos):
        if i["name"] in drop:
            continue
        if last is not None:
            ncc = float((i["thumb"] * infos[last]["thumb"]).mean())
            near = enu is None or np.linalg.norm(enu[k] - enu[last]) < DUP_GPS_M
            if ncc > DUP_NCC and near:
                drop[i["name"]] = f"téměř stejný záběr jako {infos[last]['name']}"
                continue
        last = k
    if len(drop) > MAX_DROP * n:           # something odd — keep the most important drops only
        blur = [nm for nm, r in drop.items() if r == "rozmazaná"]
        keep_budget = int(MAX_DROP * n)
        order = blur + [nm for nm in drop if nm not in blur]
        drop = {nm: drop[nm] for nm in order[:keep_budget]}
    kept = [i for i in infos if i["name"] not in drop]
    dropped = [{"name": i["name"], "reason": drop[i["name"]]} for i in infos if i["name"] in drop]
    return kept, dropped


def tone_params(kept: list[dict]) -> dict:
    """Reference exposure (median) + the global curve's shadow lift from how
    dark the set is after exposure balancing."""
    exps = [i["exposure"] for i in kept if i["exposure"]]
    ref = float(np.median(exps)) if exps else None
    return {"ref_exposure": ref}


def _srgb_to_lin(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _tone(L: np.ndarray, shadow: float) -> np.ndarray:
    """Global tone curve on display-referred luminance (sRGB-encoded, may be
    > 1 after an exposure gain): lifts shadows (≈ +0.8 EV at the darkest
    tones, a little in the mid-tones, nothing in the highlights) and rolls
    highlights off smoothly instead of clipping."""
    a = 0.9 * shadow
    Lc = np.clip(L, 0, 1)
    L = L + a * Lc * (1 - Lc) ** 3
    k = 0.85
    return np.where(L < k, L, k + (1 - k) * np.tanh((L - k) / (1 - k)))


def process_one(src: Path, dst: Path, gain: float, shadow: float, quality: int = 95) -> None:
    import cv2
    from PIL import Image
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    rgb = img[:, :, ::-1].astype(np.float32) / 255.0
    if abs(gain - 1) > 1e-3:                      # exposure balance in linear light
        lin = _srgb_to_lin(rgb) * gain
        rgb = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * np.power(np.maximum(lin, 0), 1 / 2.4) - 0.055)
    L = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    out = rgb * (_tone(L, shadow) / np.maximum(L, 1e-4))[..., None]
    mx = out.max(-1, keepdims=True)
    out = out / np.maximum(mx, 1.0)               # keep hue when a channel would clip
    im = Image.fromarray((out * 255 + 0.5).astype(np.uint8))
    kw = {"quality": quality, "subsampling": 0}
    meta = Image.open(src).info
    if meta.get("exif"):
        kw["exif"] = meta["exif"]
    if meta.get("xmp"):
        kw["xmp"] = meta["xmp"]
    im.save(dst, "JPEG", **kw)


def prepare(photos_dir: Path, out_dir: Path, shadow: float = 1.0, cull: bool = True,
            workers: int = 6, log=print) -> dict:
    """Prepared copies in out_dir; returns a report (also written as prep.json)."""
    photos_dir, out_dir = Path(photos_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # re-run of the same photo set: keep the prepared copies untouched (their
    # mtimes key Meshroom's cache — rewriting them would redo the whole SfM)
    rep_f = out_dir / "prep.json"
    if rep_f.exists():
        try:
            rep = json.loads(rep_f.read_text())
            src = sorted(p.name for p in photos_dir.iterdir() if p.suffix.lower() in IMG_EXT)
            used = set(src) - {d["name"] for d in rep["dropped"]}
            newest_src = max((photos_dir / n).stat().st_mtime for n in src)
            if (rep.get("n_input") == len(src) and rep.get("shadow_lift") == shadow
                    and all((out_dir / n).exists() for n in used)
                    and min((out_dir / n).stat().st_mtime for n in used) > newest_src):
                log("photo prep: reusing prepared photos")
                return rep
        except Exception:  # noqa: BLE001
            pass
    for f in out_dir.glob("*"):
        if f.is_file() and f.suffix.lower() in IMG_EXT:
            f.unlink()
    files = sorted(p for p in photos_dir.iterdir() if p.suffix.lower() in IMG_EXT)
    with ThreadPoolExecutor(workers) as ex:
        infos = list(ex.map(_analyse, files))
    if cull:
        kept, dropped = select(infos)
    else:
        kept, dropped = [i for i in infos if i["ok"]], []
    tp = tone_params(kept)
    ref = tp["ref_exposure"]

    def gain(i):
        # a photo exposed 2× longer is 2× brighter for the same surface → ×0.5.
        # Balanced only halfway (in EV) and within ±0.7 EV: auto-exposure swings
        # of ±2 EV are mostly the camera reacting to sky/shadow in the frame,
        # and a full correction would burn out the sunlit concrete. Never
        # brighten a photo into clipping.
        if not (ref and i["exposure"]):
            return 1.0
        g = float(np.clip((ref / i["exposure"]) ** 0.5, 0.62, 1.62))
        if g > 1:
            g = min(g, max(1.0, 0.97 / max(i.get("p99_lin", 0.5), 1e-3)))
        return g

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda i: process_one(photos_dir / i["name"], out_dir / i["name"], gain(i), shadow), kept))
    gains = [gain(i) for i in kept]
    report = {
        "n_input": len(files), "n_used": len(kept), "dropped": dropped,
        "exposure_from_metadata": sum(1 for i in kept if i["exposure"]),
        "gain_min": float(min(gains)) if gains else 1.0, "gain_max": float(max(gains)) if gains else 1.0,
        "shadow_lift": shadow,
    }
    (out_dir / "prep.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    log(f"photo prep: {len(kept)}/{len(files)} used, {len(dropped)} dropped, gains {report['gain_min']:.2f}–{report['gain_max']:.2f}")
    return report
