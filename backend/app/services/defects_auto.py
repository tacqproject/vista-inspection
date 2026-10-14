"""
Automatic defect inspection: photos → AI detections → 3D defects in the model.

  1. detect      every photo goes through the defect detector (Docker image
                 `vista-defects`, script /home/epr/VISTA/defects-docker/
                 detect_defects.py): crack segmentation at full resolution,
                 per-crack centre line, length and width in pixels; optional
                 other classes from class masks (dacl10k). Starts right after
                 upload, in parallel with the 3D reconstruction.
  2. project     once the model exists, each detection's outline / centre line
                 is ray-cast from its photo's camera onto the mesh → 3D points.
  3. merge       the same defect is seen in several photos: detections of one
                 class whose 3D points come close are one defect; the most
                 frontal, closest, most confident view represents it (its photo
                 becomes the defect's report photo). Defects seen in only one
                 photo are dropped (most false positives are view-specific).
  4. measure     crack length along the 3D centre line, width = pixel width ×
                 ground sample distance; area of the 3D outline — in metres when
                 the model scale is known (calibration, or GPS in the photos'
                 EXIF: camera positions vs. GPS positions → scale).
  5. annotate    Annotation rows (ai_detected, status "new", confidence,
                 severity proposal) — the inspector confirms or dismisses them.
                 Re-running replaces only untouched AI defects.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from app.services import storage

DETECTOR_DIR = Path(__file__).resolve().parents[3] / "defects-docker"
DETECTOR_IMAGE = os.environ.get("VISTA_DEFECTS_IMAGE", "vista-defects")
MIN_VIEWS = 2

# detector class → (VISTA defect_type, Czech name)
CLASS_MAP = {
    "crack": ("crack", "Trhlina"),
    "spalling": ("spalling", "Výlom / odprýsknutí betonu"),
    "exposed_rebar": ("corrosion", "Obnažená výztuž"),
    "corrosion": ("corrosion", "Koroze / rezavé výluhy"),
    "efflorescence": ("efflorescence", "Výkvěty solí"),
    "water_damage": ("water_damage", "Zavlhlé místo / průsak"),
    "void": ("void", "Dutina / štěrkové hnízdo"),
    "stain": ("stain", "Skvrna / povrchová degradace"),
}


def out_dir(inspection_id: str) -> Path:
    return storage.get_inspection_dir(inspection_id) / "ai_defects"


def status(inspection_id: str) -> dict:
    p = out_dir(inspection_id) / "status.json"
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "none"}


def _set_status(inspection_id: str, **kw):
    d = out_dir(inspection_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "status.tmp"
    tmp.write_text(json.dumps({"updated": time.time(), **kw}, ensure_ascii=False))
    os.replace(tmp, d / "status.json")


# ─── 1. detection ────────────────────────────────────────────────────────────

def detect(inspection_id: str, masks_dir: Path | None = None) -> Path:
    """Run the detector over all photos (blocking; call in a thread)."""
    from app.services.photogrammetry.openmvs_bridge import gpu_lock
    photos = storage.get_photos_dir(inspection_id).resolve()
    out = out_dir(inspection_id).resolve()
    out.mkdir(parents=True, exist_ok=True)
    _set_status(inspection_id, status="detecting", progress=0)
    if masks_dir is None:
        masks_dir = _run_dacl(inspection_id, photos)
    uid = f"{os.getuid()}:{os.getgid()}"
    cmd = ["docker", "run", "--rm", "--gpus", "all", "-u", uid, "-e", "HOME=/tmp",
           "-v", f"{photos}:/in:ro", "-v", f"{out}:/out", "-v", f"{DETECTOR_DIR}:/defects:ro"]
    extra = []
    if masks_dir and Path(masks_dir).exists():
        cmd += ["-v", f"{Path(masks_dir).resolve()}:/masks:ro"]
        extra = ["--masks-dir", "/masks"]
    cmd += [DETECTOR_IMAGE, "python", "/defects/detect_defects.py", "/in", "/out", *extra]
    with open(out / "detect.log", "w") as log, gpu_lock():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log.write(line); log.flush()
            if line.startswith("PROGRESS"):
                try:
                    _set_status(inspection_id, status="detecting", progress=int(line.split()[1]))
                except ValueError:
                    pass
        rc = proc.wait()
    if rc != 0 or not (out / "detections.json").exists():
        _set_status(inspection_id, status="failed", error=f"detektor skončil s kódem {rc} (viz detect.log)")
        raise RuntimeError(f"Detekce poruch selhala (kód {rc})")
    verify(inspection_id)
    _set_status(inspection_id, status="detected", progress=100)
    return out / "detections.json"


VERIFIER = Path(__file__).resolve().parents[3] / "ai" / "verifier"


def verify(inspection_id: str) -> bool:
    """Second stage: the verification classifier (ai/verifier, trained on
    manually reviewed detections) scores every detection — `verify` in
    detections.json; build_defects drops those below the threshold. Runs in
    the dacl10k env (torch + timm). Best effort: False when unavailable."""
    import shlex
    from app.config import settings
    from app.services.photogrammetry.openmvs_bridge import gpu_lock
    det = out_dir(inspection_id) / "detections.json"
    model = VERIFIER / "verifier.pt"
    cmd = settings.dacl10k_cmd.strip()
    if not (det.exists() and model.exists() and cmd):
        return False
    py = shlex.split(cmd)[0]
    photos = storage.get_photos_dir(inspection_id).resolve()
    _set_status(inspection_id, status="detecting", progress=0, stage="ověření detekcí")
    with open(out_dir(inspection_id) / "verify.log", "w") as log, gpu_lock():
        rc = subprocess.run([py, str(VERIFIER / "verify_detections.py"), str(det), str(photos), str(model)],
                            stdout=log, stderr=subprocess.STDOUT).returncode
    return rc == 0


def _run_dacl(inspection_id: str, photos: Path) -> Path | None:
    """dacl10k 19-class bridge-damage segmentation (own conda env, DACL10K_CMD)
    → class masks for the detector (graffiti veto, spalling, exposed rebar,
    rust). Optional: None when not configured or failing."""
    import shlex
    from app.config import settings
    from app.services.photogrammetry.openmvs_bridge import gpu_lock
    cmd = settings.dacl10k_cmd.strip()
    if not cmd:
        return None
    base = Path(settings.work_dir) if settings.work_dir else out_dir(inspection_id)
    out = (base / inspection_id / "dacl10k").resolve()
    out.mkdir(parents=True, exist_ok=True)
    args = shlex.split(cmd) + ["--images", str(photos), "--out", str(out),
                               "--tile", "2048", "--overlap", "256", "--save-masks"]
    try:
        with open(out / "infer.log", "w") as log, gpu_lock():
            _set_status(inspection_id, status="detecting", progress=0, stage="dacl10k")
            rc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode
        return out if rc == 0 and (out / "results.json").exists() else None
    except Exception:  # noqa: BLE001
        return None


def detect_if_needed(inspection_id: str) -> Path | None:
    """Detection unless results for the current photo set exist. Never raises
    (runs as a fire-and-forget task next to the reconstruction)."""
    f = out_dir(inspection_id) / "detections.json"
    try:
        n_photos = sum(1 for p in storage.get_photos_dir(inspection_id).iterdir()
                       if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff"))
        if f.exists() and len(json.loads(f.read_text())["images"]) >= n_photos:
            _set_status(inspection_id, status="detected", progress=100)
            return f
        return detect(inspection_id)
    except Exception as e:  # noqa: BLE001
        _set_status(inspection_id, status="failed", error=str(e)[:300])
        return None


def wait_detection(inspection_id: str, timeout_s: float = 4 * 3600) -> bool:
    """Block until the parallel detection has finished (True = results exist)."""
    t0 = time.time()
    while status(inspection_id).get("status") in ("detecting", "none", "queued") and time.time() - t0 < timeout_s:
        if status(inspection_id).get("status") == "none" and time.time() - t0 > 30:
            break                                  # nobody started it
        time.sleep(10)
    return (out_dir(inspection_id) / "detections.json").exists() and \
        status(inspection_id).get("status") != "detecting"


# ─── 2. geometry helpers ─────────────────────────────────────────────────────

def _cam_rays(cam, px: np.ndarray):
    """Pixel coords (N,2, original image px) → ray origin + unit directions
    in the MESH frame (same maths as sfm.pixel_to_mesh, vectorised)."""
    from app.services.sfm import mesh_to_sfm
    M = np.array(cam.r).reshape(3, 3)
    c = np.array(mesh_to_sfm(*cam.c))
    xn = (px[:, 0] - cam.width / 2 - cam.ppx) / cam.fx
    yn = (px[:, 1] - cam.height / 2 - cam.ppy) / cam.fy
    k1, k2, k3 = cam.dist
    xu, yu = xn.copy(), yn.copy()
    for _ in range(3):
        r2 = xu * xu + yu * yu
        f = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        xu, yu = xn / f, yn / f
    d = (M @ np.stack([xu, yu, np.ones_like(xu)])).T
    d[:, 1:] *= -1                                   # SfM → mesh frame (y,z negated)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    return c, d


def _snap_outliers(pts, normals, C, dirs, rel_tol: float = 0.12, all_points: bool = False):
    """A detection lies on ONE surface. Outline pixels along a silhouette
    (pillar edge, soffit edge) often miss it and hit the ground or deck
    metres behind — the 3D outline then balloons (a 0,3 m² rust patch came
    out as 2,5 m²). Fit a plane to the hits at the typical depth and put the
    other pixels (far hits and misses) where their rays meet that plane.
    all_points: every pixel onto the plane (area damage: its outline and area
    then follow the photo, not the mesh's few-cm noise or a wrapped corner)."""
    hit = ~np.isnan(pts[:, 0])
    if hit.sum() < 6:
        return pts, normals, 0
    t = np.full(len(pts), np.nan)
    t[hit] = np.linalg.norm(pts[hit] - C, axis=1)
    med = float(np.nanmedian(t))
    inl = hit & (np.abs(t - med) < rel_tol * med)
    if inl.sum() < max(6, 0.5 * hit.sum()):
        return pts, normals, 0
    P = pts[inl]
    p0 = P.mean(0)
    n = np.linalg.svd(P - p0, full_matrices=False)[2][2]
    out = np.ones(len(pts), bool) if all_points else ~inl
    den = dirs[out] @ n
    tt = np.where(np.abs(den) > 1e-6, ((p0 - C) @ n) / np.where(np.abs(den) > 1e-6, den, 1.0), np.nan)
    ok = (tt > 0) & (np.abs(tt - med) < 0.5 * med)
    idx = np.nonzero(out)[0][ok]
    pts = pts.copy(); normals = normals.copy()
    pts[idx] = C + tt[ok, None] * dirs[idx]
    normals[idx] = n
    return pts, normals, len(idx)


def _cast(mesh, cam, px: np.ndarray):
    """First hits of the pixels' rays: (points (N,3) with NaN for misses, face ids)."""
    c, d = _cam_rays(cam, px)
    pts = np.full((len(px), 3), np.nan)
    tri = np.full(len(px), -1)
    locs, ray_idx, tri_idx = mesh.ray.intersects_location(
        ray_origins=np.repeat(c[None], len(px), 0), ray_directions=d, multiple_hits=False)
    pts[ray_idx] = locs
    tri[ray_idx] = tri_idx
    return pts, tri, c


def _polygon_area(p: np.ndarray) -> float:
    if len(p) < 3:
        return 0.0
    c = p.mean(0)
    return float(0.5 * np.linalg.norm(np.cross(p - c, np.roll(p, -1, 0) - c), axis=1).sum())


# ─── 3. model scale from GPS ─────────────────────────────────────────────────

def _exif_gps(path: Path):
    from PIL import Image
    try:
        g = Image.open(path).getexif().get_ifd(0x8825)
    except Exception:  # noqa: BLE001
        return None
    if not g or 2 not in g or 4 not in g:
        return None
    dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    lat, lon = dms(g[2]), dms(g[4])
    if g.get(1) == "S":
        lat = -lat
    if g.get(3) == "W":
        lon = -lon
    alt = float(g[6]) if 6 in g else None
    if alt is not None and g.get(5) in (1, b"\x01"):
        alt = -alt
    return lat, lon, alt


def gps_scale(cameras, photos_dir: Path) -> dict | None:
    """Metres per MESH unit from camera centres vs. EXIF GPS (similarity fit,
    robust). None when too few geotagged photos or an unreliable fit."""
    from app.services.sfm import mesh_to_sfm
    from app.services.registration import _umeyama
    P, G = [], []
    for cam in cameras:
        g = _exif_gps(Path(photos_dir) / cam.filename)
        if g and g[2] is not None:
            P.append(mesh_to_sfm(*cam.c)); G.append(g)
    return fit_gps_scale(np.array(P), np.array(G))


def fit_gps_scale(P: np.ndarray, G: np.ndarray) -> dict | None:
    """P: camera centres (N,3, any model frame); G: (lat, lon, alt) per camera."""
    from app.services.registration import _umeyama
    if len(P) < 8:
        return None
    lat0, lon0 = G[:, 0].mean(), G[:, 1].mean()
    R = 6378137.0
    enu = np.stack([np.radians(G[:, 1] - lon0) * R * math.cos(math.radians(lat0)),
                    np.radians(G[:, 0] - lat0) * R, G[:, 2] - G[:, 2].mean()], 1)
    if np.linalg.norm(enu - enu.mean(0), axis=1).max() < 5:      # GPS noise ~ metres
        return None
    best = None
    rng = np.random.default_rng(0)
    for _ in range(200):                                          # RANSAC over similarity fits
        idx = rng.choice(len(P), 4, replace=False)
        T = _umeyama(P[idx], enu[idx])
        res = np.linalg.norm((P @ T[:3, :3].T + T[:3, 3]) - enu, axis=1)
        inl = res < 3.0
        if best is None or inl.sum() > best[1].sum():
            best = (T, inl)
    T, inl = best
    if inl.sum() < max(6, 0.5 * len(P)):
        return None
    T = _umeyama(P[inl], enu[inl])
    s = float(np.cbrt(np.linalg.det(T[:3, :3])))
    res = np.linalg.norm((P[inl] @ T[:3, :3].T + T[:3, 3]) - enu[inl], axis=1)
    return {"m_per_unit": s, "n": int(inl.sum()), "rms_m": float(np.sqrt((res ** 2).mean()))}


# ─── 4. project + merge + measure ────────────────────────────────────────────

def _landmarks_by_photo(models: Path) -> dict:
    """SfM landmarks with their observations: {photo: (pixels (N,2), X mesh frame (N,3))}."""
    f = next(iter(models.glob("**/sfm.json")), None)
    if f is None:
        return {}
    data = json.loads(f.read_text())
    view_name = {v["viewId"]: Path(v["path"]).name for v in data.get("views", [])}
    px: dict[str, list] = {}
    X: dict[str, list] = {}
    for L in data.get("structure", []):
        x = [float(v) for v in L["X"]]
        xm = (x[0], -x[1], -x[2])                      # SfM → mesh frame
        for o in L["observations"]:
            nm = view_name.get(o["observationId"])
            if nm:
                px.setdefault(nm, []).append((float(o["x"][0]), float(o["x"][1])))
                X.setdefault(nm, []).append(xm)
    return {k: (np.array(px[k]), np.array(X[k])) for k in px}


def _landmark_plane(lm, mesh, cam, C, poly: np.ndarray, allpx: np.ndarray):
    """(points, normal) on the surface the photo's own SfM points show around
    the detection, when the mesh there lies clearly behind them (missing
    geometry); None when the mesh agrees or there is too little evidence."""
    if lm is None:
        return None
    lpx, lX = lm
    x0, y0 = poly.min(0)
    x1, y1 = poly.max(0)
    m = max(80.0, 0.25 * max(x1 - x0, y1 - y0))
    sel = (lpx[:, 0] > x0 - m) & (lpx[:, 0] < x1 + m) & (lpx[:, 1] > y0 - m) & (lpx[:, 1] < y1 + m)
    if sel.sum() < 6:
        return None
    Xl = lX[sel]
    dl = np.linalg.norm(Xl - C, axis=1)
    mh, _, _ = _cast(mesh, cam, lpx[sel])
    ok = ~np.isnan(mh[:, 0])
    dm = np.linalg.norm(mh - C, axis=1)
    behind = (np.median(dm[ok] / dl[ok]) > 1.08) if ok.sum() >= 3 else True
    if not (behind or ok.mean() < 0.5):
        return None                                   # mesh agrees with the measured points
    keep = np.abs(dl - np.median(dl)) < 0.25 * np.median(dl)
    if keep.sum() < 5:
        return None
    Q = Xl[keep]
    c0 = Q.mean(0)
    _, sv, vt = np.linalg.svd(Q - c0, full_matrices=False)
    nrm = vt[2]
    if sv[2] / math.sqrt(len(Q)) > 0.03 * float(np.median(dl)):
        return None                                   # not one plane — don't guess
    _, D = _cam_rays(cam, allpx)
    den = D @ nrm
    t = np.where(np.abs(den) > 1e-6, ((c0 - C) @ nrm) / np.where(np.abs(den) > 1e-6, den, 1), np.nan)
    pts = C + t[:, None] * D
    pts[~(t > 0)] = np.nan
    return pts, nrm


_IMG_CACHE: dict = {}


def _surroundings(photo: Path, poly: np.ndarray) -> tuple[float, float]:
    """(vegetation share, surface roughness) around a detection in the photo.
    Trees/weeds and rough ground (gravel, rubble, dirt) fool the detectors;
    concrete around a real defect is comparatively smooth."""
    import cv2
    key = str(photo)
    if key not in _IMG_CACHE:
        if len(_IMG_CACHE) > 4:
            _IMG_CACHE.clear()
        img = cv2.imread(key, cv2.IMREAD_REDUCED_COLOR_4)
        _IMG_CACHE[key] = None if img is None else (
            cv2.cvtColor(img, cv2.COLOR_BGR2HSV),
            np.abs(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32), cv2.CV_32F)))
    c = _IMG_CACHE[key]
    if c is None:
        return 0.0, 0.0
    hsv, lap = c
    p = poly / 4.0
    x0, y0 = np.maximum(p.min(0) - 25, 0).astype(int)
    x1, y1 = (p.max(0) + 25).astype(int)
    sub = hsv[y0:y1, x0:x1]
    if sub.size == 0:
        return 0.0, 0.0
    h, s_ = sub[..., 0].astype(int), sub[..., 1].astype(int)
    # roughness of the RING around the detection only: a real spall is rough
    # inside (exposed aggregate) but sits in smooth concrete; gravel is rough
    # all around
    inside = np.zeros(sub.shape[:2], np.uint8)
    cv2.fillPoly(inside, [(p - [x0, y0]).astype(np.int32)], 1)
    inside = cv2.dilate(inside, np.ones((9, 9), np.uint8)).astype(bool)
    ring = lap[y0:y1, x0:x1][~inside]
    return float(((h > 28) & (h < 95) & (s_ > 45)).mean()), float(ring.mean()) if ring.size else 0.0


def _at_border(poly: np.ndarray, cam, frac: float = 0.02) -> bool:
    mx, my = frac * cam.width, frac * cam.height
    return bool(poly[:, 0].min() < mx or poly[:, 1].min() < my or
                poly[:, 0].max() > cam.width - mx or poly[:, 1].max() > cam.height - my)


def _crack_width_px(gray: np.ndarray, skel: np.ndarray, half: int = 12) -> tuple[float, float] | None:
    """Crack width measured in the photo itself (not the segmentation mask,
    which the model draws several px wider than a hairline crack): for points
    along the centre line, the brightness profile across the crack (normal to
    the local direction, 0.25 px steps) → width of the dark dip at half its
    depth below the local background. → (median, 90th percentile) in px."""
    if len(skel) < 3:
        return None
    skel = np.asarray(skel, float)
    t = np.gradient(skel, axis=0)
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    nrm = np.c_[-t[:, 1], t[:, 0]]
    o = np.arange(-half, half + 0.001, 0.25)
    xs = (skel[:, None, 0] + nrm[:, None, 0] * o[None]).astype(np.float32)
    ys = (skel[:, None, 1] + nrm[:, None, 1] * o[None]).astype(np.float32)
    prof = cv2.remap(gray, xs, ys, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
    k = len(o) // 4
    bg = np.median(np.c_[prof[:, :k], prof[:, -k:]], axis=1)
    c = len(o) // 2
    w = []
    for b, pr in zip(bg, prof):
        win = pr[c - 4 * 3:c + 4 * 3 + 1]                 # dip minimum within ±3 px of the centre line
        m = c - 12 + int(np.argmin(win))
        depth = b - pr[m]
        if depth < 6:                                     # no visible dark line here
            continue
        lvl = b - depth / 2
        i = m
        while i > 0 and pr[i] < lvl:
            i -= 1
        j = m
        while j < len(pr) - 1 and pr[j] < lvl:
            j += 1
        w.append((j - i) * 0.25)
    if len(w) < max(3, len(skel) // 4):
        return None
    return float(np.median(w)), float(np.percentile(w, 90))


def build_defects(inspection_id: str, m_per_scene: float | None, require_mesh: bool = False,
                  trace: dict | None = None) -> dict:
    """Detections → merged 3D defects (scene coordinates, metric sizes when
    m_per_scene is known). Pure computation, no DB.
    require_mesh: a model covering only PART of the photographed structure
    (full-resolution regions): where the mesh is missing the defect is not
    placed on the SfM-point plane but skipped — otherwise defects of the not
    yet computed parts float in empty space."""
    from app.services import sfm
    from app.services.registration import display_transform

    models = storage.get_models_dir(inspection_id)
    cams_file = next(iter(models.glob("**/cameras.sfm")), None)
    lod0 = next(iter(models.glob("**/model_lod0.gltf")), None)
    det_file = out_dir(inspection_id) / "detections.json"
    if not (cams_file and lod0 and det_file.exists()):
        raise RuntimeError("Chybí kamery, model nebo detekce")
    cams = {c.filename: c for c in sfm.load_cameras(cams_file)}
    s_disp, off = display_transform(lod0)
    off = np.array(off)
    m_per_mesh = m_per_scene * s_disp if m_per_scene else None
    from collections import Counter
    dropped = Counter()                       # (class, reason) → detections removed (diagnostics)
    trace = {} if trace is None else trace    # (filename, polygon start) → reason, for debugging one photo

    def _drop(d, im, reason):
        dropped[(d["cls"], reason)] += 1
        trace[(im["filename"], tuple(d["polygon"][0]))] = reason
    det_data = json.loads(det_file.read_text())
    dets = det_data["images"]
    verify_th = float((det_data.get("verifier") or {}).get("threshold", 0.5))

    # "up" from the photos: people and drones hold the camera upright, so the
    # mean camera up-axis (−Y of the camera, mesh frame) is the world vertical
    ups = []
    for cam in cams.values():
        y = np.array(cam.r).reshape(3, 3)[:, 1].copy()
        y[1:] *= -1                                   # SfM → mesh frame
        ups.append(-y)
    up = np.mean(ups, 0)
    up /= np.linalg.norm(up) or 1.0

    landmarks = _landmarks_by_photo(models)
    gray_name, gray = None, None
    photos_dir = storage.get_photos_dir(inspection_id)
    with sfm._RAY_LOCK:
        mesh = sfm._get_mesh(lod0)
        diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
        h = mesh.vertices @ up
        ground_h = float(np.percentile(h, 5)) + 0.08 * float(np.percentile(h, 99) - np.percentile(h, 1))
        low_h = float(np.percentile(h, 1)) + 0.4 * float(np.percentile(h, 99) - np.percentile(h, 1))
        items = []
        for im in dets:
            cam = cams.get(im["filename"])
            if cam is None:
                continue
            sx, sy = cam.width / im["width"], cam.height / im["height"]
            for d in im["detections"]:
                if d.get("verify") is not None and d["verify"] < verify_th:
                    _drop(d, im, "verifier"); continue                           # verification classifier: false alarm
                poly = np.array(d["polygon"], float) * [sx, sy]
                skel = np.array(d.get("skeleton") or [], float).reshape(-1, 2) * [sx, sy]
                allpx = np.vstack([poly, skel]) if len(skel) else poly
                pts, tri, C = _cast(mesh, cam, allpx)
                normals = np.where(tri[:, None] >= 0, mesh.face_normals[np.maximum(tri, 0)], np.nan)
                # The mesh can be missing exactly where defects hide (top of a
                # pillar seen from below at a grazing angle is often not
                # reconstructed): the ray then passes through the gap and hits
                # the deck behind. The SfM points this photo measured around the
                # detection tell where the surface really is — when the mesh
                # lies clearly behind them, place the defect on a plane fitted
                # to those points instead.
                P_fix = _landmark_plane(landmarks.get(im["filename"]), mesh, cam, C, poly, allpx)
                corrected = P_fix is not None
                if corrected and require_mesh:
                    _drop(d, im, "no mesh"); continue
                if corrected:
                    pts, nrm = P_fix
                    normals = np.repeat(nrm[None], len(pts), 0)
                hit = ~np.isnan(pts[:, 0])
                if hit.mean() < 0.7:
                    _drop(d, im, "off model"); continue
                if not corrected:
                    _, dirs = _cam_rays(cam, allpx)
                    pts, normals, _ = _snap_outliers(pts, normals, C, dirs, all_points=d["cls"] != "crack")
                    hit = ~np.isnan(pts[:, 0])                           # mostly off the model (sky, trees)
                P3 = pts[hit]
                depth = np.linalg.norm(P3 - C, axis=1)
                if np.percentile(depth, 90) > 1.5 * np.percentile(depth, 10) + 0.02 * diag:
                    _drop(d, im, "depth jump"); continue                           # spans a depth jump → not one surface
                n = normals[hit]
                ray = (P3 - C) / depth[:, None]
                cosv = float(np.abs((n * ray).sum(1)).mean())
                if cosv < 0.2:
                    _drop(d, im, "edge-on"); continue                           # seen almost edge-on: placement unreliable
                # terrain around the structure (twigs, ruts, cracked mud): an
                # upward-facing surface in the lowest part of the model
                n_mean = n.mean(0)
                n_mean *= np.sign(-(n_mean @ ray.mean(0))) or 1.0   # towards the camera
                up_cos = n_mean @ up / (np.linalg.norm(n_mean) or 1)
                if (P3 @ up).mean() < ground_h and up_cos > 0.7:
                    _drop(d, im, "ground"); continue
                # rubble piles, gravel, dirt around a construction site: facing
                # up in the lower part of the model (a drone set covers them too)
                if (P3 @ up).mean() < low_h and up_cos > 0.8:
                    _drop(d, im, "low+up"); continue
                # a "crack" along a sharp geometric edge (corner of a pillar,
                # formwork step) is the edge's shadow line: the surface normals
                # under it disagree, while a real crack lies on one face
                if d["cls"] == "crack":
                    nn = n * np.sign(-(n * ray).sum(1))[:, None]
                    if np.linalg.norm(nn.mean(0)) < 0.9:
                        _drop(d, im, "geometric edge"); continue
                ppoly = pts[:len(poly)][~np.isnan(pts[:len(poly), 0])]
                pskel = pts[len(poly):][~np.isnan(pts[len(poly):, 0])] if len(skel) else np.empty((0, 3))
                dmed = float(np.median(depth))
                cw = None
                if d["cls"] == "crack" and len(skel) >= 3:
                    if gray_name != im["filename"]:
                        gray_name = im["filename"]
                        gray = cv2.imread(str(photos_dir / gray_name), cv2.IMREAD_GRAYSCALE)
                    if gray is not None:
                        cw = _crack_width_px(gray, skel)
                gsd = dmed / cam.fx                     # mesh units per pixel at the defect
                if m_per_mesh:
                    gsd_m = gsd * m_per_mesh
                    # too far to tell: cracks need ≤ 5 mm/px, surface damage ≤ 15 mm/px
                    if gsd_m > (0.005 if d["cls"] == "crack" else 0.015):
                        _drop(d, im, "too far"); continue
                    if d["cls"] == "crack" and (cw[1] if cw else d.get("width_px_max", 0)) * gsd_m > 0.025:
                        _drop(d, im, "too wide"); continue                         # "crack" wider than 25 mm: an edge / joint / branch
                veg, tex = _surroundings(photos_dir / im["filename"], poly)
                # vegetation, or rough surroundings (gravel, rubble, dirt: mean
                # |Laplacian| ≈ 25 there vs ≈ 6 around real defects on concrete)
                # (roughness is in pixels: only comparable at a known scale —
                # the threshold was measured on the drone set with GPS scale;
                # close-up phone photos show plain concrete just as rough)
                if veg > 0.25 or (m_per_mesh and tex > 18):
                    _drop(d, im, "surroundings"); continue
                items.append({
                    "cls": d["cls"], "score": float(d["score"]), "photo": im["filename"],
                    "px_poly": np.array(d["polygon"], float).tolist(),
                    "pts": P3, "poly": ppoly, "skel": pskel,
                    "centroid": P3.mean(0), "cos": cosv, "depth": dmed,
                    "length": float(np.linalg.norm(np.diff(pskel, axis=0), axis=1).sum()) if len(pskel) > 1 else None,
                    # visible width (median along the crack), from the photo profile
                    "width": (cw[0] * gsd if cw else (d.get("width_px_max", 0) * gsd if d.get("width_px_max") else None)),
                    # 3D outline area, but never more than the photo allows
                    # (pixel area × GSD² / cos): a strip bent over two
                    # surfaces or a wrapped corner inflates the 3D polygon
                    "area": min(_polygon_area(ppoly),
                                1.3 * cv2.contourArea(poly.astype(np.float32)) * gsd * gsd / max(cosv, 0.2)),
                    # frontal, close, confident views represent a defect; a
                    # detection cut by the image border is a poor representative
                    "quality": float(d["score"]) * max(cosv, 0.2) / dmed * (0.3 if _at_border(poly, cam) else 1.0),
                    "corrected": corrected,
                })

    # merge: union-find over detections of one class whose 3D points come close
    # — within 8 cm when the scale is metric (1 % of a 200 m bridge model was
    # 2 m: every crack on a pillar collapsed into one defect)
    link = min(0.01 * diag, 0.08 / m_per_mesh) if m_per_mesh else 0.01 * diag
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    from scipy.spatial import cKDTree
    trees = [cKDTree(it["pts"]) for it in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a["cls"] != b["cls"] or np.linalg.norm(a["centroid"] - b["centroid"]) > 20 * link:
                continue
            # area damage (spalling, rust): the same defect seen twice has its
            # centres close together relative to its size — touching outlines
            # alone chained a large spall with a separate spot 50 cm above it
            # (through a tiny detection in between) into one "defect"
            if a["cls"] != "crack":
                r = 0.5 * math.sqrt(max(a["area"], b["area"], 0.0)) + link
                if np.linalg.norm(a["centroid"] - b["centroid"]) > r:
                    continue
            if trees[i].query(b["pts"], k=1, distance_upper_bound=link)[0].min() < link:
                parent[find(j)] = find(i)
    groups: dict[int, list] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(items[i])
    if trace is not None:
        trace["_groups"] = list(groups.values())

    to_scene = lambda p: p * s_disp + off
    defects = []
    for g in groups.values():
        photos = sorted({it["photo"] for it in g})
        if len(photos) < MIN_VIEWS:
            dropped[(g[0]["cls"], f"< {MIN_VIEWS} views (groups)")] += 1
            for it in g:
                trace[(it["photo"], tuple(it["px_poly"][0]))] = f"group seen in < {MIN_VIEWS} photos"
            continue
        # representative view: close, frontal, confident — and for area damage
        # the most complete one (a partial or tiny view must not win by a hair)
        amax = max((it["area"] or 0.0) for it in g) or 1.0
        rep = max(g, key=lambda it: it["quality"] * (1.0 if it["cls"] == "crack"
                                                    else math.sqrt(max(it["area"] or 0.0, 1e-12) / amax)))
        vtype, name = CLASS_MAP.get(rep["cls"], ("other", rep["cls"]))
        outline = rep["poly"] if len(rep["poly"]) >= 3 else rep["pts"]
        is_crack = rep["cls"] == "crack"
        length_m = rep["length"] * m_per_mesh if (is_crack and m_per_mesh and rep["length"]) else None
        width_mm = rep["width"] * m_per_mesh * 1000 if (is_crack and m_per_mesh and rep["width"]) else None
        area_m2 = rep["area"] * m_per_mesh ** 2 if (m_per_mesh and rep["area"]) else None
        if (length_m and rep["cls"] == "crack" and length_m > 6.0) or (area_m2 and rep["cls"] != "crack" and area_m2 > 3.0):
            continue                                     # implausible size: a whole surface / object, not a defect
        conf = float(np.mean([it["score"] for it in g]))
        defects.append({
            "cls": rep["cls"], "defect_type": vtype, "name": name,
            "photo": rep["photo"], "photos": photos, "confidence": conf,
            "photo_polygon": rep["px_poly"],
            "corrected": bool(rep.get("corrected")),
            "outline_scene": [dict(zip("xyz", map(float, to_scene(p)))) for p in outline],
            "centroid_scene": dict(zip("xyz", map(float, to_scene(rep["centroid"])))),
            "length_m": length_m, "width_mm": width_mm,
            "area_m2": area_m2 if rep["cls"] != "crack" else None,
            "severity": _severity(rep["cls"], width_mm, area_m2),
        })
    return {"defects": defects, "detections": len(items), "metric": m_per_mesh is not None,
            "dropped": {f"{c}: {r}": n for (c, r), n in sorted(dropped.items())}}


def _severity(cls: str, width_mm, area_m2) -> str:
    if cls == "crack":
        # width_mm here is the VISIBLE width in the photo (blur adds 2–4 px, so
        # a hairline crack shows several mm wide): the opening classes of a
        # crack gauge (0,2 / 0,5 / 1 mm) cannot be read from it — only a clearly
        # wide crack is flagged high, the rest waits for measurement on site
        if width_mm is None:
            return "medium"
        return "high" if width_mm > 8 else "medium"
    if cls == "exposed_rebar":
        return "high"
    if cls in ("spalling", "void"):
        if area_m2 is None:
            return "medium"
        return "low" if area_m2 < 0.01 else "medium" if area_m2 < 0.1 else "high"
    return "low"


def _describe(d: dict) -> str:
    parts = [f"AI návrh: {d['name'].lower()}"]
    if d["length_m"] and d["cls"] == "crack":
        parts.append(f"délka {d['length_m']:.2f} m".replace(".", ","))
    if d["width_mm"] and d["cls"] == "crack":
        parts.append("viditelná šířka na fotce ~" + f"{d['width_mm']:.0f}" + " mm (horní odhad,"
                     " neostrost fotky trhlinu rozšiřuje; rozevření ověřte měrkou trhlin)")
    if d["area_m2"]:
        parts.append(f"plocha {d['area_m2']:.3f} m²".replace(".", ","))
    txt = ", ".join(parts)
    txt += (f". Nalezeno na {len(d['photos'])} fotkách, měřeno ze snímku {d['photo']}, "
            f"jistota {round(100 * d['confidence'])} %. Ověřte a potvrďte nebo zamítněte.")
    if d.get("corrected"):
        txt += (" Síť modelu v tomto místě chybí (plocha focená z ostrého úhlu), poloha je"
                " dopočtena z bodů zaměřených na fotkách.")
    return txt


# ─── 5. annotations ──────────────────────────────────────────────────────────

async def annotate(inspection_id: str, require_mesh: bool = False) -> dict:
    """Project + merge + write Annotation rows (replaces untouched AI defects)."""
    import asyncio
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models import Annotation, Inspection
    from app.services import sfm
    from app.services.registration import display_transform

    async with AsyncSessionLocal() as db:
        insp = await db.get(Inspection, inspection_id)
        if not insp:
            raise RuntimeError("Inspekce nenalezena")
        scale_note = None
        m_per_scene = insp.scale_factor if insp.scale_factor and abs(insp.scale_factor - 1.0) > 1e-9 else None
        if m_per_scene is None:
            models = storage.get_models_dir(inspection_id)
            cams_file = next(iter(models.glob("**/cameras.sfm")), None)
            lod0 = next(iter(models.glob("**/model_lod0.gltf")), None)
            if cams_file and lod0:
                g = await asyncio.to_thread(gps_scale, sfm.load_cameras(cams_file),
                                            storage.get_photos_dir(inspection_id))
                if g and g["rms_m"] < 5.0:
                    s_disp, _ = display_transform(lod0)
                    m_per_scene = g["m_per_unit"] / s_disp
                    insp.scale_factor = m_per_scene        # whole app now measures in metres
                    scale_note = f"měřítko z GPS ({g['n']} fotek, odchylka {g['rms_m']:.1f} m)"
        result = await asyncio.to_thread(build_defects, inspection_id, m_per_scene, require_mesh)

        await db.execute(delete(Annotation).where(
            Annotation.inspection_id == inspection_id, Annotation.ai_detected.is_(True),
            Annotation.status == "new"))
        # defects the inspector already confirmed or dismissed stay decided:
        # don't propose them again (same type within ~2 % of the scene box)
        reviewed = (await db.execute(select(Annotation).where(
            Annotation.inspection_id == inspection_id, Annotation.status != "new"))).scalars().all()
        seen = [(a.defect_type, np.array([a.position_x or 0, a.position_y or 0, a.position_z or 0]))
                for a in reviewed if a.position_x is not None]
        result["defects"] = [d for d in result["defects"] if not any(
            t == d["defect_type"] and np.linalg.norm(p - np.array([d["centroid_scene"][k] for k in "xyz"])) < 0.1
            for t, p in seen)]
        for d in result["defects"]:
            c = d["centroid_scene"]
            db.add(Annotation(
                inspection_id=inspection_id, defect_type=d["defect_type"], severity=d["severity"],
                position_x=c["x"], position_y=c["y"], position_z=c["z"],
                geometry_json=d["outline_scene"] if len(d["outline_scene"]) >= 3 else None,
                description=_describe(d), report_photo=d["photo"],
                length_m=d["length_m"], area_m2=d["area_m2"],
                ai_detected=True, ai_confidence=d["confidence"], status="new",
            ))
        await db.commit()
    summary = {"n_defects": len(result["defects"]), "n_detections": result["detections"],
               "metric": result["metric"] or bool(scale_note), "scale_note": scale_note,
               "by_type": {}}
    for d in result["defects"]:
        summary["by_type"][d["name"]] = summary["by_type"].get(d["name"], 0) + 1
    _set_status(inspection_id, status="completed", progress=100, **summary)
    return summary
