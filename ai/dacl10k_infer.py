#!/usr/bin/env python3
"""
dacl10k defect-segmentation inferer for VISTA.

Runs a dacl10k-trained segmentation model over a folder of photos, tiling each
image with overlap (so cracks aren't lost to downsampling), stitching the
per-tile predictions back at full resolution, computing per-class % coverage,
and writing a colour overlay per image + a results.json that VISTA reads.

    python dacl10k_infer.py --images <dir> --out <dir> --tile 1024 --overlap 128 \
                            --device cuda [--save-masks]

RUN THIS IN THE dacl10k ENV (not VISTA's):
    /media/epr/ssd/dacl10k_env/bin/python  (Python 3.10, torch 2.0.1+cu117,
    mmcv-full 1.7.2, transformers 4.40.2). Setup: see ai/README.md.

Backends (--backend, or $DACL10K_BACKEND; default "auto")
─────────────────────────────────────────────────────────
  official   mpaques269546/dacl10k_challenge: BEiT-large ViT-Adapter +
             Mask2Former (testdev mIoU 0.47). Needs weights/models.pth — the
             author's Google-Drive folder is DEAD (404), so this is only used
             if someone supplies the checkpoint (weights/models.pth or
             $DACL10K_WEIGHTS).
  segformer  SegFormer mit-b1 from the dacl10k authors' own demo
             (huggingface.co/spaces/phiyodr/dacl-challenge, "trained on resized
             512x512 images for (only) 10 epochs"). Downloadable fallback,
             weaker model. Weights:
             weights/segformer_mitb1_dacl_space/segformer_mitb1_dacl10k_statedict.pth
  auto       official if its checkpoint exists, else segformer.

Outputs in --out
────────────────
  results.json      {"classes": [...19 names...],
                     "mask_classes": ["background", "Crack", ..., "WConccor"],
                     "model": {...backend, weights, threshold, tile...},
                     "images": [{"filename", "overlay", "coverage": {cls: frac},
                                 "width", "height",
                                 ["mask", "confidence"]  (with --save-masks)}]}
  overlay_<stem>.jpg   colour overlay (original resolution)
  mask_<stem>.png      (--save-masks) 8-bit single-channel, ORIGINAL resolution,
                       pixel value = index into "mask_classes"
                       (0 = background, 1..19 = CLASSES[value-1]).
                       A pixel gets the class with the highest probability if
                       that probability > --threshold, else 0.
  conf_<stem>.png      (--save-masks) 8-bit single-channel, ORIGINAL resolution,
                       round(255 * max class probability). For background
                       pixels it is the (sub-threshold) best class probability,
                       so VISTA can re-threshold: class pixels are >= thr*255.
Masks are in the raw stored pixel orientation (EXIF orientation NOT applied).

Progress: `PROGRESS <pct> <msg>` lines on stdout.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# This file lives both in ai/ and in ai/dacl10k_challenge/ — make `import model`
# and ./weights resolve to the repo in either case.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE if (_HERE / "model").is_dir() else _HERE / "dacl10k_challenge"
sys.path.insert(0, str(_REPO))

WEIGHTS_DIR = _REPO / "weights"
SEGFORMER_WEIGHTS = WEIGHTS_DIR / "segformer_mitb1_dacl_space" / "segformer_mitb1_dacl10k_statedict.pth"

CLASSES = [
    "Crack", "ACrack", "Wetspot", "Efflorescence", "Rust", "Rockpocket",
    "Hollowareas", "Cavity", "Spalling", "Graffiti", "Weathering",
    "Restformwork", "ExposedRebars", "Bearing", "EJoint", "Drainage",
    "PEquipment", "JTape", "WConccor",
]
MASK_CLASSES = ["background"] + CLASSES
# Distinct-ish RGB per class for the overlay (index 0 = background → unused).
PALETTE = np.array([
    (0, 0, 0), (239, 68, 68), (249, 115, 22), (234, 179, 8), (132, 204, 22),
    (34, 197, 94), (20, 184, 166), (6, 182, 212), (59, 130, 246), (99, 102, 241),
    (139, 92, 246), (168, 85, 247), (217, 70, 239), (236, 72, 153), (244, 63, 94),
    (120, 113, 108), (161, 98, 7), (5, 150, 105), (2, 132, 199), (147, 51, 234),
], dtype=np.uint8)
IMG_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)


# ─── Model backends ──────────────────────────────────────────────────────────
# Each backend: .predict_probs(tile: PIL RGB) -> torch.FloatTensor [19, h, w]
# of per-class probabilities in [0, 1] (multi-label), on the model device.

def _to_tensor(tile: Image.Image, device):
    import torch
    a = np.asarray(tile, dtype=np.float32) / 255.0
    a = (a - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
    return torch.from_numpy(a.transpose(2, 0, 1).copy()).unsqueeze(0).to(device)


class OfficialBackend:
    """mpaques269546/dacl10k_challenge: BEiT-L adapter + Mask2Former (512 px)."""
    name = "official"

    def __init__(self, weights: str, device: str):
        import torch
        from model import load_segmenter        # dacl10k_challenge/model package
        self.torch = torch
        self.device = device
        self.weights = weights
        self.net = load_segmenter(weights).to(device).eval()

    def predict_probs(self, tile):
        with self.torch.no_grad():
            out = self.net(_to_tensor(tile, self.device))[0]   # [19,h,w] probs
        return out.float().clamp_(0, 1)


class SegformerBackend:
    """SegFormer mit-b1 from the dacl10k authors' HF demo (512x512, sigmoid)."""
    name = "segformer"

    def __init__(self, weights: str, device: str):
        import torch
        from transformers import SegformerConfig, SegformerForSemanticSegmentation
        self.torch = torch
        self.device = device
        self.weights = weights
        ck = torch.load(weights, map_location="cpu")
        cfg = SegformerConfig(**{k: v for k, v in ck["config"].items()
                                 if k not in ("transformers_version",)})
        net = SegformerForSemanticSegmentation(cfg)
        # saved from the demo's SegModel wrapper → strip the "segformer." prefix
        sd = {k[len("segformer."):]: v for k, v in ck["state_dict"].items()}
        net.load_state_dict(sd, strict=True)
        self.net = net.to(device).eval()
        self.size = 512

    def predict_probs(self, tile):
        torch = self.torch
        import torch.nn.functional as F
        w, h = tile.size
        x = _to_tensor(tile.resize((self.size, self.size), Image.BILINEAR), self.device)
        with torch.no_grad():
            logits = self.net(pixel_values=x).logits              # [1,19,128,128]
            logits = F.interpolate(logits, size=(h, w), mode="bilinear",
                                   align_corners=False)
        return logits[0].sigmoid()


def _find_official_weights():
    env = os.environ.get("DACL10K_WEIGHTS", "")
    if env and "segformer" not in Path(env).name.lower():
        return env if Path(env).is_file() else None
    cands = [WEIGHTS_DIR / "models.pth"] + sorted(WEIGHTS_DIR.glob("*.pth")) \
        + sorted(WEIGHTS_DIR.glob("*.pt"))
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def load_model(device: str, backend: str = "auto"):
    if device.startswith("cuda"):
        import torch
        if not torch.cuda.is_available():
            print("WARN CUDA nedostupná → běžím na CPU", flush=True)
            device = "cpu"
    backend = (backend or "auto").lower()
    if backend in ("auto", "official"):
        w = _find_official_weights()
        if w:
            return OfficialBackend(w, device)
        if backend == "official":
            raise FileNotFoundError(
                "Oficiální váhy (weights/models.pth) nenalezeny – Drive odkaz autora je "
                "mrtvý (404). Použij --backend segformer nebo dodej DACL10K_WEIGHTS.")
    env = os.environ.get("DACL10K_WEIGHTS", "")
    w = env if env and "segformer" in Path(env).name.lower() else str(SEGFORMER_WEIGHTS)
    if not Path(w).is_file():
        raise FileNotFoundError(f"Váhy SegFormer nenalezeny: {w} (viz ai/README.md)")
    return SegformerBackend(w, device)


# ─── Tiling / stitching / coverage / overlay ─────────────────────────────────

def _tile_positions(size: int, tile: int, overlap: int):
    if size <= tile:
        return [0]
    stride = max(1, tile - overlap)
    pos = list(range(0, size - tile + 1, stride))
    if pos[-1] != size - tile:
        pos.append(size - tile)
    return pos


def segment_image(model, img: Image.Image, tile: int, overlap: int, thr: float):
    """Full-resolution (label uint8 HxW, conf uint8 HxW) via overlapping tiles.

    Per tile: p = per-class probs; conf = max_c p; label = argmax+1 if conf > thr
    else 0. Overlaps: the tile with the higher conf wins (so damage beats
    background, and the more confident class beats a weaker one).
    """
    import torch
    W, H = img.size
    label = np.zeros((H, W), dtype=np.uint8)
    conf = np.zeros((H, W), dtype=np.uint8)
    for y in _tile_positions(H, tile, overlap):
        for x in _tile_positions(W, tile, overlap):
            x1, y1 = min(x + tile, W), min(y + tile, H)
            probs = model.predict_probs(img.crop((x, y, x1, y1)))   # [19,h,w]
            pmax, arg = probs.max(dim=0)
            t_lab = torch.where(pmax > thr, arg + 1, torch.zeros_like(arg))
            t_conf = (pmax * 255).round_().clamp_(0, 255)
            t_lab = t_lab.to(torch.uint8).cpu().numpy()
            t_conf = t_conf.to(torch.uint8).cpu().numpy()
            r_lab, r_conf = label[y:y1, x:x1], conf[y:y1, x:x1]
            win = t_conf > r_conf
            r_lab[win] = t_lab[win]
            r_conf[win] = t_conf[win]
    # a pixel whose best conf is sub-threshold must be background even if it was
    # written by a tile (can't happen by construction, but keep it explicit)
    label[conf <= int(thr * 255)] = 0
    return label, conf


def coverage_of(mask: np.ndarray) -> dict:
    total = mask.size or 1
    out = {c: 0.0 for c in CLASSES}
    counts = np.bincount(mask.ravel(), minlength=len(MASK_CLASSES))
    for i in range(1, len(MASK_CLASSES)):
        out[CLASSES[i - 1]] = round(float(counts[i]) / total, 4)
    return out


def make_overlay(img: Image.Image, mask: np.ndarray, alpha: float = 0.45) -> Image.Image:
    base = np.asarray(img.convert("RGB"), dtype=np.float32)
    colour = PALETTE[np.clip(mask, 0, len(CLASSES)).astype(int)].astype(np.float32)
    blend = np.where((mask > 0)[..., None],
                     base * (1 - alpha) + colour * alpha, base)
    return Image.fromarray(blend.astype(np.uint8))


def _acquire_gpu_lock(path: str):
    """Optional: hold an flock on `path` while the model is on the GPU."""
    import fcntl
    f = open(path, "a")
    print(f"PROGRESS 1 Čekám na GPU zámek {path}…", flush=True)
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tile", type=int, default=1024)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--backend", default=os.environ.get("DACL10K_BACKEND", "auto"),
                    choices=["auto", "official", "segformer"])
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="per-class probability threshold (dacl10k standard 0.5)")
    ap.add_argument("--save-masks", action="store_true",
                    default=os.environ.get("DACL10K_SAVE_MASKS", "") not in ("", "0"),
                    help="write mask_<stem>.png (class index) + conf_<stem>.png")
    ap.add_argument("--gpu-lock", default=os.environ.get("DACL10K_GPU_LOCK", ""),
                    help="flock this file while running (e.g. /tmp/vista-gpu.lock). "
                         "Do NOT combine with an outer `flock` on the same file.")
    ap.add_argument("--force-tile", type=int, default=int(os.environ.get("DACL10K_FORCE_TILE", 0) or 0),
                    help="override --tile (VISTA always passes --tile 1024; put this in "
                         "DACL10K_CMD to change it). 0 = use --tile")
    ap.add_argument("--force-overlap", type=int, default=int(os.environ.get("DACL10K_FORCE_OVERLAP", -1)),
                    help="override --overlap (-1 = use --overlap)")
    args = ap.parse_args()
    if args.force_tile > 0:
        args.tile = args.force_tile
    if args.force_overlap >= 0:
        args.overlap = args.force_overlap

    images_dir, out_dir = Path(args.images), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in images_dir.glob("*") if f.suffix.lower() in IMG_EXT)
    total = len(files) or 1

    _lock = _acquire_gpu_lock(args.gpu_lock) if (args.gpu_lock and
                                                 args.device.startswith("cuda")) else None

    print(f"PROGRESS 2 Načítám model ({args.device}, backend {args.backend})…", flush=True)
    model = load_model(args.device, args.backend)
    print(f"INFO backend={model.name} weights={model.weights} device={model.device}", flush=True)

    results = {
        "classes": CLASSES,
        "mask_classes": MASK_CLASSES,
        "model": {
            "backend": model.name,
            "weights": str(model.weights),
            "description": ("BEiT-L ViT-Adapter + Mask2Former (mpaques269546)"
                            if model.name == "official" else
                            "SegFormer mit-b1, dacl10k authors' HF demo (10 epochs @512)"),
            "threshold": args.threshold,
            "tile": args.tile, "overlap": args.overlap,
        },
        "mask_format": ({"mask": "8-bit PNG, value = index into mask_classes (0=background)",
                         "confidence": "8-bit PNG, round(255*max class probability)",
                         "resolution": "original image, raw pixel orientation (no EXIF rotate)"}
                        if args.save_masks else None),
        "images": [],
    }
    for i, f in enumerate(files):
        print(f"PROGRESS {int(5 + 90 * i / total)} Segmentuji {f.name} ({i + 1}/{len(files)})",
              flush=True)
        try:
            img = Image.open(f).convert("RGB")
        except Exception as e:
            print(f"WARN nelze načíst {f.name}: {e}", flush=True)
            continue
        t0 = time.time()
        mask, conf = segment_image(model, img, args.tile, args.overlap, args.threshold)
        overlay_name = f"overlay_{f.stem}.jpg"
        make_overlay(img, mask).save(out_dir / overlay_name, "JPEG", quality=85)
        entry = {"filename": f.name, "overlay": overlay_name,
                 "coverage": coverage_of(mask),
                 "width": img.width, "height": img.height}
        if args.save_masks:
            entry["mask"] = f"mask_{f.stem}.png"
            entry["confidence"] = f"conf_{f.stem}.png"
            Image.fromarray(mask, mode="L").save(out_dir / entry["mask"], optimize=True)
            Image.fromarray(conf, mode="L").save(out_dir / entry["confidence"])
        entry["seconds"] = round(time.time() - t0, 2)
        results["images"].append(entry)
        print(f"INFO {f.name}: {entry['seconds']} s", flush=True)

    (out_dir / "results.json").write_text(json.dumps(results))
    if str(model.device).startswith("cuda"):
        import torch
        print(f"INFO peak VRAM (torch) {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB", flush=True)
    print("PROGRESS 100 Hotovo", flush=True)


if __name__ == "__main__":
    main()
