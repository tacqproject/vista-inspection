"""
Render a reconstructed model from the exact pose + lens of one of its source
photos (Open3D offscreen, EGL) — the most honest visual check of a
reconstruction: the undistorted photo, the textured model and the shaded bare
geometry side by side, from the same viewpoint. Two reconstructions of the same
photo set can be compared directly even though their coordinate frames differ.

CLI:  python -m app.services.mesh_render <mesh.obj> <cameras.sfm> <photo> <out.jpg> [photos_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


def render_from_photo(mesh_path: Path, cameras_sfm: Path, photo_name: str, out: Path,
                      photos_dir: Path | None = None, width: int = 1200) -> Path:
    import cv2
    photo, textured, shaded = render_panels(mesh_path, cameras_sfm, photo_name, photos_dir, width)
    panels = [p for p in (photo, textured, shaded) if p is not None]
    cv2.imwrite(str(out), cv2.cvtColor(np.hstack(panels), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Path(out)


BACKGROUND = (31, 41, 59)          # render background (RGB) — "no model here"


def texture_fidelity(photo: np.ndarray, textured: np.ndarray) -> dict:
    """How faithfully the textured model reproduces the photo from its own pose:
    `ncc` = correlation of fine detail (high-pass) — drops with blur, ghosting
    and misalignment; `sharpness` = detail energy of the render relative to the
    photo (1 = as sharp as the photo); `coverage` = share of the frame covered."""
    import cv2
    mask = np.abs(textured.astype(int) - BACKGROUND).sum(2) > 12
    mask = cv2.erode(mask.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    if mask.sum() < 1000:
        return {"ncc": None, "sharpness": None, "coverage": float(mask.mean())}
    g = lambda im: cv2.cvtColor(im, cv2.COLOR_RGB2GRAY).astype(np.float32)
    hp = lambda x: x - cv2.GaussianBlur(x, (0, 0), 2.0)
    a, b = hp(g(photo))[mask], hp(g(textured))[mask]
    ncc = float(((a - a.mean()) * (b - b.mean())).mean() / (a.std() * b.std() + 1e-6))
    return {"ncc": ncc, "sharpness": float(b.std() / (a.std() + 1e-6)), "coverage": float(mask.mean())}


def render_panels(mesh_path: Path, cameras_sfm: Path, photo_name: str,
                  photos_dir: Path | None = None, width: int = 1200):
    """(undistorted photo | None, textured render, shaded geometry render) as RGB arrays."""
    import cv2
    import open3d as o3d
    from open3d.visualization import rendering
    from app.services import sfm

    cam = next(c for c in sfm.load_cameras(Path(cameras_sfm)) if c.filename == photo_name)
    s = width / cam.width
    W, H = width, int(round(cam.height * s))
    K = np.array([[cam.fx * s, 0, (cam.width / 2 + cam.ppx) * s],
                  [0, cam.fy * s, (cam.height / 2 + cam.ppy) * s], [0, 0, 1]])
    # mesh frame = SfM frame with y,z negated (S);  x_cam = Rᵀ(S·X − C)
    M = np.array(cam.r).reshape(3, 3)
    S = np.diag([1.0, -1.0, -1.0])
    E = np.eye(4)
    E[:3, :3] = M.T @ S
    E[:3, 3] = -M.T @ np.array(cam.c)

    r = rendering.OffscreenRenderer(W, H)
    r.scene.set_background([0.12, 0.16, 0.23, 1.0])
    model = o3d.io.read_triangle_model(str(mesh_path))
    for m in model.materials:
        m.shader = "defaultUnlit"
        m.base_color = [1.0, 1.0, 1.0, 1.0]   # AliceVision's MTL has Kd 0.6 — would darken the texture
    r.scene.add_model("tex", model)
    r.setup_camera(K, E, W, H)
    textured = np.asarray(r.render_to_image())

    r.scene.clear_geometry()
    geo = o3d.io.read_triangle_mesh(str(mesh_path))
    geo.compute_vertex_normals()
    mat = rendering.MaterialRecord()
    mat.shader = "defaultLit"
    mat.base_color = [0.8, 0.8, 0.8, 1.0]
    r.scene.add_geometry("geo", geo, mat)
    r.scene.scene.set_sun_light([0.3, -0.5, -0.8], [1, 1, 1], 60000)
    r.scene.scene.enable_sun_light(True)
    r.setup_camera(K, E, W, H)
    shaded = np.asarray(r.render_to_image())

    photo = None
    if photos_dir:
        img = cv2.imread(str(Path(photos_dir) / photo_name))
        if img is not None:
            Kf = np.array([[cam.fx, 0, cam.width / 2 + cam.ppx], [0, cam.fy, cam.height / 2 + cam.ppy], [0, 0, 1]])
            k1, k2, k3 = cam.dist
            und = cv2.undistort(img, Kf, np.array([k1, k2, 0, 0, k3]), None, Kf)
            photo = cv2.cvtColor(cv2.resize(und, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    return photo, textured, shaded

if __name__ == "__main__":
    a = sys.argv[1:]
    render_from_photo(Path(a[0]), Path(a[1]), a[2], Path(a[3]), Path(a[4]) if len(a) > 4 else None)
    print(a[3])
