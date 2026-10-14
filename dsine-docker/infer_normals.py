"""DSINE normals for every undistorted image of a COLMAP TXT scene.
usage: python infer_normals.py <scene dir with images/ + sparse/> <out dir> [width]
Writes <out>/<image stem>.npy: float16 (3, H, W) camera-frame unit normals
(DSINE convention) at the given width, with the true (scaled) intrinsics."""
import sys, os
from pathlib import Path
import numpy as np, torch, cv2

scene, out = Path(sys.argv[1]), Path(sys.argv[2])
width = int(sys.argv[3]) if len(sys.argv) > 3 else 1344
out.mkdir(parents=True, exist_ok=True)
cams = {}
for l in (scene / "sparse/cameras.txt").read_text().splitlines():
    if l.startswith("#") or not l.strip():
        continue
    p = l.split()
    assert p[1] == "PINHOLE", p[1]
    cams[p[0]] = [float(v) for v in p[2:8]]            # w h fx fy cx cy
lines = [l for l in (scene / "sparse/images.txt").read_text().splitlines() if not l.startswith("#")]
imgs = [(lines[i].split()[9], lines[i].split()[8]) for i in range(0, len(lines), 2) if lines[i].strip()]

pred = torch.hub.load("/dsine", "DSINE", source="local", trust_repo=True,
                      local_file_path="/dsine/checkpoints/dsine.pt")
for name, cid in imgs:
    dst = out / (Path(name).stem + ".npy")
    if dst.exists():
        continue
    w, h, fx, fy, cx, cy = cams[cid]
    s = width / w
    im = cv2.imread(str(scene / "images" / name))
    im = cv2.resize(im, (width, int(round(h * s))), interpolation=cv2.INTER_AREA)
    t = torch.from_numpy(cv2.cvtColor(im, cv2.COLOR_BGR2RGB).astype(np.float32) / 255).permute(2, 0, 1)[None]
    K = torch.tensor([[[fx * s, 0, cx * s], [0, fy * s, cy * s], [0, 0, 1]]], dtype=torch.float32, device="cuda")
    with torch.inference_mode():
        n = pred.infer_tensor(t, K)[0]
    np.save(dst, n.cpu().numpy().astype(np.float16))
    print(name, tuple(n.shape), flush=True)
