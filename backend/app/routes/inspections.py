from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Inspection, Structure, Annotation, Measurement, PhotoSet
from app.schemas import InspectionCreate, InspectionUpdate, InspectionOut, PhotoSetOut
from app.services.auth import get_current_user, ROLE_RANK
from app.models import User

router = APIRouter(prefix="/structures/{structure_id}/inspections", tags=["inspections"])


async def _model_lod(inspection_id: str, lod: int, db: AsyncSession):
    """
    Absolute path to an inspection's model at the requested LOD, based on the
    photo_set's ACTUAL model_path — which may be a cropped variant
    (model_crop…_lodN.gltf). Using a fixed model_lod0.gltf glob instead breaks
    on cropped models: the viewer shows the crop but the backend would read the
    original, so scene↔mesh coordinates no longer match (alignment goes wild).
    """
    import re
    from app.config import settings

    r = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    ps = r.scalar_one_or_none()
    if not ps or not ps.model_path:
        raise HTTPException(400, "Model není k dispozici — nejprve dokončete rekonstrukci")
    rel = re.sub(r"lod\d", f"lod{lod}", ps.model_path)
    p = settings.upload_path / rel
    if not p.exists():
        # fall back to the un-suffixed lod file next to it
        alt = next(iter(p.parent.glob(f"**/*lod{lod}.gltf")), None)
        if alt:
            return alt
        raise HTTPException(400, "Soubor modelu nenalezen")
    return p


async def _get_structure_or_404(structure_id: str, db: AsyncSession) -> Structure:
    s = await db.get(Structure, structure_id)
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")
    return s


async def _enrich(insp: Inspection, db: AsyncSession) -> InspectionOut:
    r_ann = await db.execute(
        select(func.count(Annotation.id)).where(Annotation.inspection_id == insp.id)
    )
    ann_count = r_ann.scalar_one()

    r_meas = await db.execute(
        select(func.count(Measurement.id)).where(Measurement.inspection_id == insp.id)
    )
    meas_count = r_meas.scalar_one()

    # photo_set is already eagerly loaded via selectinload — safe to access
    item = InspectionOut.model_validate(insp)
    item.photo_set = PhotoSetOut.model_validate(insp.photo_set) if insp.photo_set else None
    item.annotation_count = ann_count
    item.measurement_count = meas_count
    return item


def _with_photo_set():
    """Reusable selectinload option for Inspection.photo_set."""
    return selectinload(Inspection.photo_set)


class AlignIn(BaseModel):
    ref_inspection_id: str


class PointPair(BaseModel):
    a: dict   # {x, y, z} on the reference model (scene coords)
    b: dict   # {x, y, z} on this model (scene coords)


class AlignManualIn(BaseModel):
    ref_inspection_id: str
    pairs: list[PointPair]


@router.post("/{inspection_id}/align", status_code=202)
async def align_inspection(
    structure_id: str,
    inspection_id: str,
    body: AlignIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Register this inspection's model onto a reference inspection of the same
    structure. Enables comparing a PARTIAL re-scan in the reference's frame.
    Works best when both inspections have a calibrated scale ("Kalibrovat" on a
    measurement): the known scale removes the similarity ambiguity. Without it
    the service falls back to a scale search, which is slower and can fail.
    """
    await _get_structure_or_404(structure_id, db)
    insp = await db.get(Inspection, inspection_id)
    ref = await db.get(Inspection, body.ref_inspection_id)
    if not insp or not ref or insp.structure_id != structure_id or ref.structure_id != structure_id:
        raise HTTPException(404, "Inspekce nenalezena nebo nepatří k této konstrukci")
    if insp.id == ref.id:
        raise HTTPException(400, "Nelze zarovnat inspekci samu na sebe")

    insp.align_ref_id = ref.id
    insp.align_status = "processing"
    insp.align_error = None
    insp.align_matrix = None
    insp.align_fitness = None
    insp.align_rmse = None
    await db.commit()

    background_tasks.add_task(_run_alignment, inspection_id, ref.id)
    return {"status": "processing"}


@router.post("/{inspection_id}/align-manual", status_code=200)
async def align_manual(
    structure_id: str,
    inspection_id: str,
    body: AlignManualIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Align by user-picked correspondences: the client sends ≥3 matching points
    (scene coords) on the reference (a) and this model (b). We fit a similarity
    transform through them (+ gentle ICP polish). Deterministic and independent
    of calibration — the reliable route for a small detail onto a big model.
    """
    import numpy as np
    from app.services import storage, registration

    await _get_structure_or_404(structure_id, db)
    insp = await db.get(Inspection, inspection_id)
    ref = await db.get(Inspection, body.ref_inspection_id)
    if not insp or not ref or insp.structure_id != structure_id or ref.structure_id != structure_id:
        raise HTTPException(404, "Inspekce nenalezena nebo nepatří k této konstrukci")
    if insp.id == ref.id:
        raise HTTPException(400, "Nelze zarovnat inspekci samu na sebe")
    if len(body.pairs) < 3:
        raise HTTPException(400, "Zadejte alespoň 3 dvojice odpovídajících bodů")

    try:
        mesh_b = await _model_lod(inspection_id, 0, db)   # actual (maybe cropped) model
        mesh_a = await _model_lod(ref.id, 0, db)
        # scene coords → mesh coords (the picks come from the viewer's scene box)
        s_b, o_b = registration.display_transform(mesh_b)
        s_a, o_a = registration.display_transform(mesh_a)
        b_pts = [[(p.b["x"] - o_b[0]) / s_b, (p.b["y"] - o_b[1]) / s_b, (p.b["z"] - o_b[2]) / s_b] for p in body.pairs]
        a_pts = [[(p.a["x"] - o_a[0]) / s_a, (p.a["y"] - o_a[1]) / s_a, (p.a["z"] - o_a[2]) / s_a] for p in body.pairs]

        result = await asyncio_to_thread(
            registration.align_from_points,
            b_pts, a_pts, mesh_b, mesh_a, ref.scale_factor,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Ruční zarovnání selhalo: {e}")

    insp.align_ref_id = ref.id
    insp.align_matrix = result["matrix"]
    insp.align_fitness = result["fitness"]
    insp.align_rmse = result["rmse"]
    insp.align_status = "done"
    insp.align_error = None
    await db.commit()
    return {
        "status": "done", "fitness": result["fitness"], "rmse": result["rmse"],
        "warning": result.get("warning"),
        "pair_residuals": result.get("pair_residuals"),
        "metric": result.get("metric"),
    }


async def asyncio_to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)


# ─── Timeline: best photo of one spot across every inspection ──────────────────

class TimelinePointIn(BaseModel):
    inspection_id: str                   # inspection the point was clicked in
    x: float; y: float; z: float         # modelPoint in that viewer's frame
    canonical: bool = False              # is that inspection baked (frame = ref scene)
    nx: float | None = None; ny: float | None = None; nz: float | None = None


def _photos_of_point(inspection_id: str, mx: float, my: float, mz: float,
                     normal, occlude: bool, limit: int = 6):
    """Best source photos of a point (given in this inspection's mesh frame)."""
    from app.services import sfm, storage

    models_dir = storage.get_models_dir(inspection_id)
    sfm_files = list(models_dir.glob("**/cameras.sfm"))
    if not sfm_files:
        return []
    parent = sfm_files[0].parent
    mesh = next((parent / f"model_{lod}.gltf" for lod in ("lod0", "lod1", "lod2")
                 if (parent / f"model_{lod}.gltf").exists()), None)

    # `occlude` marks a NON-canonical inspection, whose gltf lives in the same
    # (original) mesh frame as the mapped point — so we can read the true surface
    # normal off the mesh and back-verify each marker against it. For a canonical
    # (baked) inspection the gltf is in the ref-scene frame while the point comes
    # in the inspection's ORIGINAL frame (via inv(align)), so the mesh no longer
    # matches: skip both mesh steps and trust the alignment + transformed normal.
    use_mesh = occlude and mesh is not None

    n = None
    if use_mesh:
        # Normal from the TARGET mesh at the mapped point — more reliable than the
        # alignment-transformed click normal (which compounds the alignment error).
        nm = sfm.surface_normal(mesh, mx, my, mz)
        if nm:
            n = sfm.mesh_to_sfm(*nm)
    if n is None and normal is not None:
        # normalize: the alignment transform (a similarity with scale) stretches
        # the direction, which would otherwise skew the projection angle filter.
        import numpy as np
        v = np.asarray(normal, float)
        ln = float(np.linalg.norm(v))
        if ln > 1e-9:
            n = sfm.mesh_to_sfm(*(v / ln))

    sx, sy, sz = sfm.mesh_to_sfm(mx, my, mz)
    cameras = sfm.load_cameras(sfm_files[0])
    matches = sfm.project_point(cameras, sx, sy, sz, normal=n)
    if not matches:
        return []

    # Verify each marker actually lands on the mapped point. Cross-inspection
    # matching relies on the alignment; a bad alignment maps the spot elsewhere —
    # back-projection then drops those cameras, so the timeline shows NOTHING for
    # that inspection instead of garbage photos. Only possible when the mesh frame
    # matches the point (non-canonical); see `use_mesh` above.
    if use_mesh:
        try:
            matches = sfm.filter_by_backprojection(matches[:150], cameras, mesh, mx, my, mz)
        except Exception:
            pass
    return matches[:limit]


@router.post("/timeline-photos", status_code=200)
async def timeline_photos(
    structure_id: str, body: TimelinePointIn, db: AsyncSession = Depends(get_db),
):
    """
    For a clicked spot, return the best source photo of THAT SAME physical spot
    from every inspection of the structure (date-ordered) — a degradation
    timeline. The point is mapped through the reference frame + each inspection's
    alignment into that inspection's own camera space.
    """
    import numpy as np
    from app.services import registration

    struct = await _get_structure_or_404(structure_id, db)
    ref_id = struct.reference_inspection_id or body.inspection_id

    s_ref, o_ref = 1.0, np.zeros(3)
    try:
        s_ref, o = registration.display_transform(await _model_lod(ref_id, 0, db))
        o_ref = np.asarray(o, float)
    except HTTPException:
        pass

    p = np.array([body.x, body.y, body.z], float)
    # normal only if the full triple is present — np.array([1.0, None, None], float)
    # would raise (TypeError → 500).
    has_normal = None not in (body.nx, body.ny, body.nz)
    n = np.array([body.nx, body.ny, body.nz], float) if has_normal else None

    # clicked point → reference-mesh frame
    if body.inspection_id == ref_id:
        p_ref, n_ref = p, n
    elif body.canonical:                              # point is in ref SCENE coords
        p_ref, n_ref = (p - o_ref) / s_ref, n         # scale+offset → direction unchanged
    else:
        insp = await db.get(Inspection, body.inspection_id)
        M = np.array(insp.align_matrix, float).reshape(4, 4) if insp and insp.align_matrix else np.eye(4)
        p_ref = M[:3, :3] @ p + M[:3, 3]
        n_ref = M[:3, :3] @ n if n is not None else None

    rows = (await db.execute(
        select(Inspection).where(Inspection.structure_id == structure_id)
    )).scalars().all()

    items = []
    for x in rows:
        if x.id == ref_id:
            px, nx = p_ref, n_ref
        elif (x.align_ref_id == ref_id and x.align_matrix
              and (x.align_fitness or 0) >= registration.MIN_FITNESS):
            # Only inspections whose alignment registration itself deemed
            # trustworthy. A failed alignment (fitness ~0) maps the spot to the
            # wrong 3D place; the projection is then self-consistent but lands on
            # a nonsensical part of the photo — and back-projection can't catch it
            # (it verifies occlusion, not the alignment). So gate here, hard.
            Minv = np.linalg.inv(np.array(x.align_matrix, float).reshape(4, 4))
            px = Minv[:3, :3] @ p_ref + Minv[:3, 3]
            nx = Minv[:3, :3] @ n_ref if n_ref is not None else None
        else:
            continue                                   # unaligned / untrustworthy → skip
        normal = tuple(float(v) for v in nx) if nx is not None else None
        found = await asyncio_to_thread(
            _photos_of_point, x.id, float(px[0]), float(px[1]), float(px[2]),
            normal, not bool(x.canonical), 6,
        )
        if found:
            items.append({
                "inspection_id": x.id, "name": x.name, "date": str(x.inspection_date),
                "photos": [
                    {"filename": m["filename"], "u": m["u"], "v": m["v"],
                     "distance": m.get("distance")}
                    for m in found
                ],
            })

    items.sort(key=lambda it: it["date"])
    return {"items": items}


class AdjustOverlayIn(BaseModel):
    tx: float; ty: float; tz: float      # translation in A's scene frame
    rx: float; ry: float; rz: float      # rotation (radians, XYZ) about B's centre
    sc: float = 1.0                      # uniform scale about B's centre


def _euler_xyz(x: float, y: float, z: float):
    """3×3 rotation for THREE.js Euler order 'XYZ' (R = Rx·Ry·Rz)."""
    import numpy as np
    cx, sx = np.cos(x), np.sin(x); cy, sy = np.cos(y), np.sin(y); cz, sz = np.cos(z), np.sin(z)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rx @ Ry @ Rz


@router.post("/{inspection_id}/adjust-overlay", status_code=200)
async def adjust_overlay(
    structure_id: str, inspection_id: str, body: AdjustOverlayIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Fold a manual overlay fine-tune (a rotation about B's centre + a translation,
    both in A's scene frame — exactly what the overlay sliders apply) into the
    stored mesh-space alignment matrix, so it persists.
    """
    import numpy as np
    from app.services import registration

    await _get_structure_or_404(structure_id, db)
    insp = await db.get(Inspection, inspection_id)
    if not insp or insp.structure_id != structure_id:
        raise HTTPException(404, "Inspekce nenalezena")
    if not insp.align_ref_id or not insp.align_matrix:
        raise HTTPException(400, "Inspekce není zarovnaná")
    if insp.canonical:
        raise HTTPException(400, "Zapečený (kanonický) model nelze takto dolaďovat")

    a_model = await _model_lod(insp.align_ref_id, 0, db)   # ref (A), overlay basis
    b_model = await _model_lod(inspection_id, 1, db)        # overlay loads B's lod1
    s_a, o_a = registration.display_transform(a_model)
    o_a = np.asarray(o_a, float)
    M = np.array(insp.align_matrix, float).reshape(4, 4)   # B mesh → A mesh

    # B's centre in A's scene frame — the rotation pivot the UI used
    lo, hi = registration.gltf_bounds(b_model)
    bc = (np.asarray(lo, float) + np.asarray(hi, float)) / 2.0
    wc = s_a * (M[:3, :3] @ bc + M[:3, 3]) + o_a

    R = _euler_xyz(body.rx, body.ry, body.rz) * (body.sc or 1.0)   # scale ∘ rotate
    t = np.array([body.tx, body.ty, body.tz], float)
    A_adj = np.eye(4); A_adj[:3, :3] = R; A_adj[:3, 3] = wc + t - R @ wc

    D_a = np.eye(4); D_a[:3, :3] = np.eye(3) * s_a; D_a[:3, 3] = o_a
    D_a_inv = np.eye(4); D_a_inv[:3, :3] = np.eye(3) / s_a; D_a_inv[:3, 3] = -o_a / s_a
    M_new = D_a_inv @ A_adj @ D_a @ M                       # new B mesh → A mesh

    insp.align_matrix = [float(v) for v in M_new.flatten()]
    await db.commit()
    return {"status": "ok"}


# ─── Canonical frame: bake every inspection into the reference's coordinates ────

class SetReferenceIn(BaseModel):
    reference_inspection_id: str


@router.post("/set-reference", status_code=200)
async def set_reference(
    structure_id: str, body: SetReferenceIn,
    background: BackgroundTasks, db: AsyncSession = Depends(get_db),
):
    """
    Make one inspection the structure's canonical frame. Every OTHER completed
    inspection is (auto-aligned if needed and) baked into that frame so views,
    coordinates and scale line up across inspections. Runs in the background.
    """
    struct = await _get_structure_or_404(structure_id, db)
    ref = await db.get(Inspection, body.reference_inspection_id)
    if not ref or ref.structure_id != structure_id:
        raise HTTPException(404, "Referenční inspekce nenalezena")
    struct.reference_inspection_id = ref.id
    # the reference itself defines the frame — it's canonical without baking
    ref.canonical = False
    await db.commit()
    background.add_task(_run_canonicalization, structure_id, ref.id)
    return {"status": "processing", "reference_inspection_id": ref.id}


def _sO_to_mat(s: float, o) -> "object":
    import numpy as np
    M = np.eye(4)
    M[0, 0] = M[1, 1] = M[2, 2] = s
    M[:3, 3] = np.asarray(o, float)
    return M


def _xform_point(T, x, y, z):
    import numpy as np
    v = T[:3, :3] @ np.array([x, y, z], float) + T[:3, 3]
    return float(v[0]), float(v[1]), float(v[2])


def _xform_json_points(T, data):
    """Transform a polygon/points blob (list of {x,y,z} or [x,y,z]) by T."""
    if not isinstance(data, list):
        return data
    out = []
    for p in data:
        if isinstance(p, dict) and {"x", "y", "z"} <= set(p):
            x, y, z = _xform_point(T, p["x"], p["y"], p["z"])
            q = dict(p); q["x"], q["y"], q["z"] = x, y, z
            out.append(q)
        elif isinstance(p, (list, tuple)) and len(p) >= 3:
            x, y, z = _xform_point(T, p[0], p[1], p[2])
            out.append([x, y, z, *list(p[3:])])
        else:
            out.append(p)
    return out


async def _run_canonicalization(structure_id: str, reference_id: str):
    import asyncio
    import numpy as np
    from app.database import AsyncSessionLocal
    from app.services import registration, canonicalize

    async with AsyncSessionLocal() as db:
        ref = await db.get(Inspection, reference_id)
        if not ref:
            return
        try:
            ref_model = await _model_lod(reference_id, 0, db)
        except HTTPException:
            return
        s_ref, o_ref = registration.display_transform(ref_model)
        D_ref = _sO_to_mat(s_ref, o_ref)                 # ref mesh → canonical scene

        rows = (await db.execute(
            select(Inspection).where(
                Inspection.structure_id == structure_id,
                Inspection.id != reference_id,
            )
        )).scalars().all()

        for insp in rows:
            try:
                insp_model = await _model_lod(insp.id, 0, db)
            except HTTPException:
                continue                                  # no model yet — skip

            # 1) make sure it's aligned to the reference (auto-align if not)
            aligned = (insp.align_ref_id == reference_id
                       and insp.align_status == "done" and insp.align_matrix)
            if not aligned:
                try:
                    new1 = await _model_lod(insp.id, 1, db)
                    ref1 = await _model_lod(reference_id, 1, db)
                    res = await asyncio.to_thread(
                        registration.register, new1, ref1,
                        insp.scale_factor, ref.scale_factor,
                    )
                except Exception:
                    continue
                if not res.get("ok"):
                    insp.align_ref_id = reference_id
                    insp.align_status = "failed"
                    insp.align_error = "Auto-zarovnání k referenci selhalo — zkuste ruční"
                    await db.commit()
                    continue
                insp.align_ref_id = reference_id
                insp.align_matrix = res["matrix"]
                insp.align_fitness = res["fitness"]
                insp.align_rmse = res["rmse"]
                insp.align_status = "done"
                insp.align_error = None

            if insp.canonical:
                continue                                  # already baked

            # 2) bake the model into the canonical frame, and migrate annotations
            Mm = np.array(insp.align_matrix, float).reshape(4, 4)   # insp mesh → ref mesh
            bake = D_ref @ Mm                                        # insp mesh → canonical scene
            # scene→scene migration for stored annotation/measurement coords
            T = np.array(
                registration.mesh_matrix_to_scene(insp.align_matrix, insp_model, ref_model),
                float,
            ).reshape(4, 4)
            try:
                await asyncio.to_thread(
                    canonicalize.bake_inspection_models, insp_model.parent, bake,
                )
            except Exception as e:  # noqa: BLE001
                insp.align_error = f"Zapečení selhalo: {e}"
                await db.commit()
                continue

            anns = (await db.execute(
                select(Annotation).where(Annotation.inspection_id == insp.id)
            )).scalars().all()
            for a in anns:
                if a.position_x is not None:
                    a.position_x, a.position_y, a.position_z = _xform_point(
                        T, a.position_x, a.position_y, a.position_z)
                if a.geometry_json:
                    a.geometry_json = _xform_json_points(T, a.geometry_json)

            meas = (await db.execute(
                select(Measurement).where(Measurement.inspection_id == insp.id)
            )).scalars().all()
            for m in meas:
                if m.points_json:
                    m.points_json = _xform_json_points(T, m.points_json)

            insp.canonical = True
            await db.commit()


async def _run_alignment(inspection_id: str, ref_id: str):
    import asyncio
    from app.database import AsyncSessionLocal
    from app.services import storage, registration

    async with AsyncSessionLocal() as db:
        insp = await db.get(Inspection, inspection_id)
        ref = await db.get(Inspection, ref_id)
        if not insp or not ref:
            return
        try:
            mesh_new = await _model_lod(inspection_id, 1, db)   # actual (maybe cropped) model
            mesh_ref = await _model_lod(ref_id, 1, db)
            result = await asyncio.to_thread(
                registration.register,
                mesh_new, mesh_ref,
                insp.scale_factor, ref.scale_factor,
            )
            insp.align_matrix = result["matrix"]
            insp.align_fitness = result["fitness"]
            insp.align_rmse = result["rmse"]
            if result["ok"]:
                insp.align_status = "done"
            else:
                insp.align_status = "failed"
                hint = (
                    "Zkontrolujte, zda se skeny skutečně překrývají."
                    if result.get("metric")
                    else "Zkalibrujte měřítko obou inspekcí (panel Měření → Kalibrovat) "
                         "a zkuste to znovu — bez měřítka je zarovnání méně spolehlivé."
                )
                insp.align_error = (
                    f"Zarovnání je nespolehlivé (překryv {result['fitness']:.0%}). {hint}"
                )
        except Exception as e:  # noqa: BLE001
            insp.align_status = "failed"
            insp.align_error = str(e)
        await db.commit()


def _coverage_ids(anns_a: list[dict], scene_matrix: list[float], b_mesh_path, thresh: float) -> set:
    """
    Which A defects lie inside the area B scanned. B's mesh is mapped into A's
    SCENE frame (B mesh → B scene → A scene via the scene-space matrix); an A
    defect is "covered" if some B surface point is within ~1.5× the match
    tolerance of it. Lets a partial B avoid flagging A defects it never saw as
    "resolved".
    """
    import numpy as np
    from app.services import registration

    # fast bbox + vertices straight from the gltf (no slow texture decode)
    lo, hi = registration.gltf_bounds(b_mesh_path)              # B mesh → B scene
    center = (lo + hi) / 2
    max_dim = float((hi - lo).max()) or 1.0
    s_b = 5.0 / max_dim
    o_b = -center * s_b
    vb = registration.gltf_vertices(b_mesh_path)
    vb_scene = np.asarray(vb, dtype=float) * s_b + o_b

    m = np.array(scene_matrix, dtype=float).reshape(4, 4)        # B scene → A scene
    vh = np.c_[vb_scene, np.ones(len(vb_scene))]
    va = (m @ vh.T).T
    va = va[:, :3] / va[:, 3:4]

    cov_r = (thresh or 0.6) * 1.5
    ids: set = set()
    for adef in anns_a:
        if adef.get("position_x") is None:
            continue
        p = np.array([adef["position_x"], adef["position_y"], adef["position_z"]], dtype=float)
        if float(np.min(np.linalg.norm(va - p, axis=1))) <= cov_r:
            ids.add(adef["id"])
    return ids


@router.get("/compare")
async def compare_inspections(
    structure_id: str,
    a: str,
    b: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Compare defects of two inspections (a = older, b = newer) of one structure.
    Positions are matched heuristically across the differing reconstruction
    frames — see services/inspection_compare.py.
    """
    from app.schemas import AnnotationOut
    from app.services.inspection_compare import compare_annotations

    await _get_structure_or_404(structure_id, db)
    insp_a = await db.get(Inspection, a)
    insp_b = await db.get(Inspection, b)
    if not insp_a or not insp_b or insp_a.structure_id != structure_id or insp_b.structure_id != structure_id:
        raise HTTPException(404, "Inspekce nenalezena nebo nepatří k této konstrukci")

    async def annotations_of(iid: str) -> list[dict]:
        r = await db.execute(select(Annotation).where(Annotation.inspection_id == iid))
        return [AnnotationOut.model_validate(x).model_dump(mode="json") for x in r.scalars().all()]

    # If B is registered onto A, match defects by true distance in a common
    # frame. Annotations live in SCENE coordinates (the viewer's normalized
    # box), so the mesh-space registration matrix has to be rebased first.
    from app.services import registration, storage

    anns_a = await annotations_of(a)
    anns_b = await annotations_of(b)

    matrix = None
    thresh = None
    covered_ids = None
    # Once B is baked into A's canonical frame its annotations already live in
    # A's scene coordinates — the mapping is the identity, no matrix to apply.
    if insp_b.canonical and insp_b.align_ref_id == a:
        import numpy as np
        matrix = list(np.eye(4).flatten())
        thresh = 0.6 / (insp_a.scale_factor or 1.0)
        covered_ids = _coverage_ids(anns_a, matrix, await _model_lod(b, 1, db), thresh)
    elif insp_b.align_ref_id == a and insp_b.align_status == "done" and insp_b.align_matrix:
        try:
            # lod0 = the mesh the viewer actually loads (via model_path, which may
            # be a cropped variant), so its bbox / scene normalization matches
            matrix = registration.mesh_matrix_to_scene(
                insp_b.align_matrix,
                await _model_lod(b, 0, db), await _model_lod(a, 0, db),
            )
            # 0.6 m tolerance expressed in A's scene units
            thresh = 0.6 / (insp_a.scale_factor or 1.0)
            # B is often only PART of the structure: work out which A defects
            # actually fall inside the region B scanned, so unmatched A defects
            # outside it aren't misreported as "resolved". lod1 is plenty here
            # (and ~10× faster to load than lod0); its bbox ≈ lod0's.
            covered_ids = _coverage_ids(anns_a, matrix, await _model_lod(b, 1, db), thresh)
        except Exception:
            matrix = None

    result = compare_annotations(
        anns_a, anns_b,
        matrix=matrix, match_threshold=thresh, covered_ids=covered_ids,
    )
    result["aligned"] = matrix is not None
    result["scene_matrix"] = matrix   # B_scene → A_scene (for camera sync/overlay)
    result["inspection_a"] = {"id": a, "name": insp_a.name, "date": str(insp_a.inspection_date)}
    result["inspection_b"] = {"id": b, "name": insp_b.name, "date": str(insp_b.inspection_date)}
    return result


@router.get("", response_model=list[InspectionOut])
async def list_inspections(structure_id: str, db: AsyncSession = Depends(get_db)):
    await _get_structure_or_404(structure_id, db)
    result = await db.execute(
        select(Inspection)
        .where(Inspection.structure_id == structure_id)
        .options(_with_photo_set())
        .order_by(Inspection.inspection_date.desc())
    )
    return [await _enrich(i, db) for i in result.scalars().all()]


@router.post("", response_model=InspectionOut, status_code=201)
async def create_inspection(
    structure_id: str, data: InspectionCreate, db: AsyncSession = Depends(get_db)
):
    await _get_structure_or_404(structure_id, db)
    insp = Inspection(structure_id=structure_id, **data.model_dump())
    db.add(insp)
    await db.commit()

    # Re-query with eagerly loaded relationship
    result = await db.execute(
        select(Inspection).where(Inspection.id == insp.id).options(_with_photo_set())
    )
    insp = result.scalar_one()
    return await _enrich(insp, db)


@router.get("/{inspection_id}", response_model=InspectionOut)
async def get_inspection(structure_id: str, inspection_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Inspection)
        .where(Inspection.id == inspection_id, Inspection.structure_id == structure_id)
        .options(_with_photo_set())
    )
    insp = result.scalar_one_or_none()
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    return await _enrich(insp, db)


@router.patch("/{inspection_id}", response_model=InspectionOut)
async def update_inspection(
    structure_id: str, inspection_id: str, data: InspectionUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Inspection)
        .where(Inspection.id == inspection_id, Inspection.structure_id == structure_id)
        .options(_with_photo_set())
    )
    insp = result.scalar_one_or_none()
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")

    # exclude_unset (not exclude_none): InspectionBase carries defaults
    # (status="draft", scale_factor=1.0, inspection_kind="bezna") — with
    # exclude_none a partial update such as saving the model orientation silently
    # reset the calibrated scale and the inspection status.
    for k, v in data.model_dump(exclude_unset=True).items():
        if k in ("name", "inspection_date") and v is None:
            continue                        # required columns
        setattr(insp, k, v)
    await db.commit()

    result = await db.execute(
        select(Inspection).where(Inspection.id == inspection_id).options(_with_photo_set())
    )
    insp = result.scalar_one()
    return await _enrich(insp, db)


class ApprovalIn(BaseModel):
    action: str   # submit | approve | reject | reopen


@router.post("/{inspection_id}/approval", response_model=InspectionOut)
async def set_approval(
    structure_id: str, inspection_id: str, body: ApprovalIn,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Report approval workflow: inspectors submit, admins approve/reject."""
    result = await db.execute(
        select(Inspection)
        .where(Inspection.id == inspection_id, Inspection.structure_id == structure_id)
        .options(_with_photo_set())
    )
    insp = result.scalar_one_or_none()
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")

    from datetime import datetime, timezone
    rank = ROLE_RANK.get(user.role, -1)
    action = body.action

    if action == "submit":
        if rank < ROLE_RANK["inspector"]:
            raise HTTPException(403, "Nedostatečná oprávnění")
        insp.approval_status = "submitted"
        insp.approved_by = None
        insp.approved_at = None
    elif action in ("approve", "reject"):
        if rank < ROLE_RANK["admin"]:
            raise HTTPException(403, "Schvalovat může pouze administrátor")
        insp.approval_status = "approved" if action == "approve" else "rejected"
        insp.approved_by = user.username
        insp.approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elif action == "reopen":
        if rank < ROLE_RANK["inspector"]:
            raise HTTPException(403, "Nedostatečná oprávnění")
        insp.approval_status = "draft"
        insp.approved_by = None
        insp.approved_at = None
    else:
        raise HTTPException(400, "Neznámá akce")

    await db.commit()
    result = await db.execute(
        select(Inspection).where(Inspection.id == inspection_id).options(_with_photo_set())
    )
    return await _enrich(result.scalar_one(), db)


@router.post("/{inspection_id}/share", response_model=InspectionOut)
async def create_share(
    structure_id: str, inspection_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Mint (or reuse) a read-only share token for this inspection's protocol."""
    if ROLE_RANK.get(user.role, -1) < ROLE_RANK["inspector"]:
        raise HTTPException(403, "Nedostatečná oprávnění")
    result = await db.execute(
        select(Inspection)
        .where(Inspection.id == inspection_id, Inspection.structure_id == structure_id)
        .options(_with_photo_set())
    )
    insp = result.scalar_one_or_none()
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    if not insp.share_token:
        import secrets
        insp.share_token = secrets.token_urlsafe(16)
        await db.commit()
        result = await db.execute(
            select(Inspection).where(Inspection.id == inspection_id).options(_with_photo_set())
        )
        insp = result.scalar_one()
    return await _enrich(insp, db)


@router.delete("/{inspection_id}/share", response_model=InspectionOut)
async def revoke_share(
    structure_id: str, inspection_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    if ROLE_RANK.get(user.role, -1) < ROLE_RANK["inspector"]:
        raise HTTPException(403, "Nedostatečná oprávnění")
    result = await db.execute(
        select(Inspection)
        .where(Inspection.id == inspection_id, Inspection.structure_id == structure_id)
        .options(_with_photo_set())
    )
    insp = result.scalar_one_or_none()
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    insp.share_token = None
    await db.commit()
    result = await db.execute(
        select(Inspection).where(Inspection.id == inspection_id).options(_with_photo_set())
    )
    return await _enrich(result.scalar_one(), db)


@router.delete("/{inspection_id}", status_code=204)
async def delete_inspection(structure_id: str, inspection_id: str, db: AsyncSession = Depends(get_db)):
    insp = await db.get(Inspection, inspection_id)
    if not insp or insp.structure_id != structure_id:
        raise HTTPException(404, "Inspekce nenalezena")
    await db.delete(insp)          # cascades to annotations/measurements/photo_set
    await db.commit()

    # Best-effort cleanup of the inspection's files on disk (photos, 3D model).
    import shutil
    from app.services.storage import get_inspection_dir
    try:
        d = get_inspection_dir(inspection_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
