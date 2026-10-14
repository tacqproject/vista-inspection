"""
Automatic defect detection on inspection photos → per-photo instances in pixels.

    python detect_defects.py <images dir> <out dir> [--tile 512] [--overlap 96]
                             [--thr 0.5] [--masks-dir <dir with class masks>]

Cracks: SegFormer (crack-segmentation fine-tune) run tile-by-tile at full,
0.75× and 0.5× resolution (max), hysteresis threshold — hairline cracks are 1–3 px wide in a 12 MP photo and vanish when
the image is downscaled. Each connected crack becomes one instance with its
skeleton (centre line, ordered), length and width in pixels (width = 2 × the
distance-transform value on the skeleton).

Other damage classes can be supplied as per-image class masks (e.g. from the
dacl10k model: `mask_<stem>.png` + "mask_classes" in its results.json) — they
are converted to instances by the same code (outline polygon, area).

Output: <out>/detections.json
  {"images": [{"filename", "width", "height", "overlay",
               "detections": [{"cls", "score", "polygon": [[x,y]..],
                               "skeleton": [[x,y]..], "length_px",
                               "width_px_max", "width_px_med", "area_px"}]}]}
and overlay_<stem>.jpg for review. Progress: "PROGRESS <pct> <msg>" lines.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
# dacl10k class masks → VISTA detector classes. Only classes that looked
# reliable on real bridge photos; cracks come from the dedicated crack model,
# Graffiti is used to veto crack candidates inside paint.
DACL_MAP = {"Spalling": "spalling", "ExposedRebars": "exposed_rebar", "Rust": "corrosion"}
COLORS = {"crack": (0, 0, 255), "spalling": (0, 165, 255), "exposed_rebar": (255, 0, 255),
          "corrosion": (0, 90, 200), "efflorescence": (255, 255, 0), "water_damage": (255, 128, 0),
          "void": (128, 0, 255), "stain": (0, 200, 200), "other": (200, 200, 200)}


def crack_probability(model, img_rgb: np.ndarray, tile: int, overlap: int, device) -> np.ndarray:
    import torch
    H, W = img_rgb.shape[:2]
    step = tile - overlap
    prob = np.zeros((H, W), np.float32)
    weight = np.zeros((H, W), np.float32)
    # cosine window: tile borders count less (less context there)
    w1 = np.hanning(tile).astype(np.float32) + 1e-3
    win = np.outer(w1, w1)
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    ys = list(range(0, max(H - tile, 0) + 1, step)) + ([H - tile] if H > tile and (H - tile) % step else [])
    xs = list(range(0, max(W - tile, 0) + 1, step)) + ([W - tile] if W > tile and (W - tile) % step else [])
    batch, where = [], []

    def flush():
        if not batch:
            return
        x = torch.from_numpy(np.stack(batch)).permute(0, 3, 1, 2).to(device)
        with torch.inference_mode():
            logits = model(pixel_values=x).logits
            logits = torch.nn.functional.interpolate(logits, size=(tile, tile), mode="bilinear",
                                                     align_corners=False)
            p = torch.softmax(logits, 1)[:, 1].float().cpu().numpy()
        for (y, xx), pi in zip(where, p):
            prob[y:y + tile, xx:xx + tile] += pi * win
            weight[y:y + tile, xx:xx + tile] += win
        batch.clear(); where.clear()

    for y in ys:
        for x in xs:
            t = img_rgb[y:y + tile, x:x + tile].astype(np.float32) / 255.0
            batch.append((t - mean) / std)
            where.append((y, x))
            if len(batch) == 16:
                flush()
    flush()
    return prob / np.maximum(weight, 1e-6)


def order_skeleton(sk: np.ndarray) -> list[tuple[int, int]]:
    """Skeleton pixels of one component → a polyline along its longest path
    (two BFS passes: farthest point from anywhere, then farthest from that)."""
    pts = np.argwhere(sk)
    if len(pts) == 0:
        return []
    idx = {(int(y), int(x)): i for i, (y, x) in enumerate(pts)}
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def bfs(start):
        from collections import deque
        prev = {start: None}
        q = deque([start])
        last = start
        while q:
            p = q.popleft(); last = p
            for dy, dx in nbrs:
                n = (p[0] + dy, p[1] + dx)
                if n in idx and n not in prev:
                    prev[n] = p; q.append(n)
        return last, prev

    a, _ = bfs(tuple(map(int, pts[0])))
    b, prev = bfs(a)
    path = []
    p = b
    while p is not None:
        path.append((p[1], p[0]))          # (x, y)
        p = prev[p]
    return path


def polyline_length(path) -> float:
    if len(path) < 2:
        return 0.0
    a = np.asarray(path, np.float32)
    return float(np.linalg.norm(np.diff(a, axis=0), axis=1).sum())


def reject_reason(img_bgr: np.ndarray, comp: np.ndarray, y0, y1, x0, x1) -> str | None:
    """Cheap sanity filters for a crack candidate (colour of its pixels vs. its
    surroundings): vegetation is green, spray paint is saturated, a real crack
    is darker than the concrete around it."""
    sub = img_bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    px = hsv[comp]
    h, s, v = px[:, 0].astype(float), px[:, 1].astype(float), px[:, 2].astype(float)
    if np.mean((h > 30) & (h < 90) & (s > 50)) > 0.4:
        return "vegetation"
    # spray paint is saturated — but so is a crack with rust bleeding out of
    # it (orange/ochre hue): that one is the more serious defect, keep it
    if np.median(s) > 90 and not (5 <= np.median(h) <= 28 and np.median(s) < 200):
        return "paint"
    ring = cv2.dilate(comp.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool) & ~comp
    if ring.any() and np.median(v) > np.median(hsv[ring][:, 2]) - 8:
        return "not darker than surroundings"
    return None


def multiscale_probability(model, img_rgb: np.ndarray, tile: int, overlap: int, device,
                           scales=(1.0, 0.75, 0.5)) -> np.ndarray:
    """Max over image scales. The crack model was trained on cracks a few px
    wide: at full drone resolution a network of fine cracks comes out as weak,
    broken fragments, at 0.75× / 0.5× the same cracks are continuous and
    confident (measured on pillar photos; 2×/3× upscaling only weakened them)."""
    H, W = img_rgb.shape[:2]
    prob = None
    for s in scales:
        im = img_rgb if s == 1.0 else cv2.resize(img_rgb, (round(W * s), round(H * s)), interpolation=cv2.INTER_AREA)
        if min(im.shape[:2]) < tile:
            continue
        p = crack_probability(model, im, tile, overlap, device)
        if s != 1.0:
            p = cv2.resize(p, (W, H), interpolation=cv2.INTER_LINEAR)
        prob = p if prob is None else np.maximum(prob, p)
    return prob


def hysteresis(prob: np.ndarray, thr: float, thr_low: float) -> np.ndarray:
    """Pixels > thr_low connected to a seed > thr: a fine crack is followed
    along its weak parts instead of falling apart into short fragments."""
    low = (prob > thr_low).astype(np.uint8)
    n, lab = cv2.connectedComponents(low, connectivity=8)
    seeds = np.unique(lab[prob > thr])
    keep = np.zeros(n, bool); keep[seeds[seeds > 0]] = True
    return keep[lab].astype(np.uint8)


def crack_instances(prob: np.ndarray, thr: float, min_len_px: float, img_bgr=None,
                    thr_low: float | None = None) -> list[dict]:
    from skimage.morphology import skeletonize
    mask = hysteresis(prob, thr, thr_low) if thr_low else (prob > thr).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # bridge small gaps along a crack before splitting into instances
    joined = cv2.dilate(mask, np.ones((7, 7), np.uint8))
    n, lab = cv2.connectedComponents(joined)
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    out = []
    for k in range(1, n):
        comp = (lab == k) & (mask > 0)
        if comp.sum() < 30:
            continue
        ys, xs = np.nonzero(comp)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub = comp[y0:y1, x0:x1]
        sk = skeletonize(sub)
        path = [(x + x0, y + y0) for x, y in order_skeleton(sk)]
        L = polyline_length(path)
        if L < min_len_px:
            continue
        widths = 2 * dist[y0:y1, x0:x1][sk]
        if img_bgr is not None and reject_reason(img_bgr, sub, y0, y1, x0, x1):
            continue
        # elongation L²/area: a crack is a line (≫ 10); a compact dark blob the
        # crack model fires on is a hole — spalling / void, not a crack
        cls = "crack" if L * L / max(sub.sum(), 1) > 12 else "spalling"
        if cls == "spalling" and sub.sum() < 2e-4 * prob.size:
            continue                      # pores / pebbles: too small to report
        # simplify centre line to ≤ ~60 vertices
        step = max(1, len(path) // 60)
        spath = path[::step] + ([path[-1]] if path[-1] != path[::step][-1] else [])
        cnts, _ = cv2.findContours(sub.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cnts, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, 1.5, True)[:, 0, :] + [x0, y0]
        out.append({
            "cls": cls,
            "score": float(prob[y0:y1, x0:x1][sub].mean()),
            "polygon": c.astype(int).tolist(),
            "skeleton": [[int(x), int(y)] for x, y in spath],
            "length_px": round(L, 1),
            "width_px_max": round(float(np.percentile(widths, 95)) if len(widths) else 0.0, 2),
            "width_px_med": round(float(np.median(widths)) if len(widths) else 0.0, 2),
            "area_px": int(sub.sum()),
        })
    return out


def mask_instances(mask: np.ndarray, cls: str, min_area_px: int, score: float = 0.5) -> list[dict]:
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    out = []
    for k in range(1, n):
        if stats[k, cv2.CC_STAT_AREA] < min_area_px:
            continue
        comp = (lab == k).astype(np.uint8)
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cnts, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, 3.0, True)[:, 0, :]
        if len(c) < 3:
            continue
        out.append({"cls": cls, "score": score, "polygon": c.astype(int).tolist(),
                    "area_px": int(stats[k, cv2.CC_STAT_AREA])})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images")
    ap.add_argument("out")
    ap.add_argument("--tile", type=int, default=512)
    ap.add_argument("--overlap", type=int, default=96)
    ap.add_argument("--thr", type=float, default=0.5)
    ap.add_argument("--thr-low", type=float, default=0.25, help="hysteresis: follow a crack down to this probability (0 = off)")
    ap.add_argument("--scales", type=float, nargs="+", default=[1.0, 0.75, 0.5])
    ap.add_argument("--min-len", type=float, default=60.0, help="min crack length in px")
    ap.add_argument("--masks-dir", default=None, help="class masks from another model (dacl10k)")
    ap.add_argument("--only", nargs="*", default=None, help="process only these filenames")
    a = ap.parse_args()

    import torch
    from transformers import SegformerForSemanticSegmentation
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SegformerForSemanticSegmentation.from_pretrained("/models/crack_segformer").to(device).eval()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in Path(a.images).iterdir() if p.suffix.lower() in IMG_EXT)
    if a.only:
        files = [p for p in files if p.name in set(a.only)]
    extra = {}
    if a.masks_dir and (Path(a.masks_dir) / "results.json").exists():
        r = json.loads((Path(a.masks_dir) / "results.json").read_text())
        extra = {"classes": list(r.get("mask_classes") or [])}

    images = []
    for i, f in enumerate(files):
        img = cv2.imread(str(f))
        if img is None:
            continue
        H, W = img.shape[:2]
        prob = multiscale_probability(model, cv2.cvtColor(img, cv2.COLOR_BGR2RGB), a.tile, a.overlap, device, a.scales)
        dets = crack_instances(prob, a.thr, a.min_len, img, a.thr_low or None)
        mp = Path(a.masks_dir or "/nonexistent") / f"mask_{f.stem}.png"
        if extra and mp.exists():
            cm = cv2.imread(str(mp), cv2.IMREAD_UNCHANGED)
            if cm.shape[:2] != (H, W):
                cm = cv2.resize(cm, (W, H), interpolation=cv2.INTER_NEAREST)
            names = extra["classes"]
            if "Graffiti" in names:            # crack candidates drawn in paint
                g = cm == names.index("Graffiti")
                g = cv2.dilate(g.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
                keep = []
                for d in dets:
                    pm = np.zeros((H, W), np.uint8)
                    cv2.fillPoly(pm, [np.array(d["polygon"], np.int32)], 1)
                    if pm.sum() == 0 or (g & (pm > 0)).sum() / pm.sum() < 0.3:
                        keep.append(d)
                dets = keep
            for idx, name in enumerate(names):
                vcls = DACL_MAP.get(name)
                if vcls:
                    dets += mask_instances(cm == idx, vcls, min_area_px=int(0.0004 * H * W))
        ov = img.copy()
        for d in dets:
            col = COLORS.get(d["cls"], COLORS["other"])
            cv2.polylines(ov, [np.array(d["polygon"], np.int32)], True, col, 3)
            if d.get("skeleton"):
                cv2.polylines(ov, [np.array(d["skeleton"], np.int32)], False, (0, 255, 0), 2)
        ov_name = f"overlay_{f.stem}.jpg"
        cv2.imwrite(str(out / ov_name), cv2.resize(ov, (W // 2, H // 2)), [cv2.IMWRITE_JPEG_QUALITY, 80])
        images.append({"filename": f.name, "width": W, "height": H, "overlay": ov_name, "detections": dets})
        (out / "detections.json").write_text(json.dumps({"images": images}))
        print(f"PROGRESS {int(100 * (i + 1) / len(files))} {f.name}: {len(dets)} nálezů", flush=True)
    (out / "detections.json").write_text(json.dumps({"images": images}))


if __name__ == "__main__":
    sys.exit(main())
