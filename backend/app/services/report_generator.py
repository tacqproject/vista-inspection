"""
PDF report generator using ReportLab.
Generates inspection reports for structures.
"""

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph,
    Spacer, Table, TableStyle, HRFlowable, Image, PageBreak,
    KeepTogether,
)
from reportlab.platypus.flowables import BalancedColumns

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

from app.config import settings

# ─── Fonts — a Unicode TTF so Czech diacritics (č ř ž š ě ů ď ť ň) render ──────
# ReportLab's built-in Helvetica is Latin-1 only and mangles Czech letters.

FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
try:
    _fd = Path("/usr/share/fonts/truetype/dejavu")
    pdfmetrics.registerFont(TTFont(FONT, str(_fd / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_fd / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
except Exception:                       # last-resort fallback (accents may break)
    FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"


# ─── Representative defect photo ───────────────────────────────────────────────

def _report_photo_path(a: dict) -> Optional[Path]:
    """Resolve the on-disk image chosen to represent a defect in the protocol.

    Preference: the explicitly picked detail photo (``report_photo``) → the
    annotation screenshot (``image_path``) → the first uploaded detail photo.
    """
    insp_id = a.get("inspection_id")
    ann_id = a.get("id")
    detail_dir = None
    if insp_id and ann_id:
        detail_dir = settings.upload_path / "inspections" / insp_id / "annotation_images" / ann_id

    rp = a.get("report_photo")
    if rp and detail_dir is not None:
        cand = detail_dir / Path(rp).name
        if cand.is_file():
            return cand

    ip = a.get("image_path")
    if ip:
        cand = settings.upload_path / ip
        if cand.is_file():
            return cand

    if detail_dir is not None and detail_dir.is_dir():
        for f in sorted(detail_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return f
    return None


def _defect_outline_projection(a: dict, cache: dict):
    """Pick the source photo that best sees this defect and project its 3D
    polygon (``geometry_json``) into that photo.

    Returns ``(photo_path, [{u, v} | None, ...])`` in the photo's normalized image
    space, or ``None`` when there are no cameras / the defect is not visible.
    ``geometry_json`` is in the viewer's SCENE frame; the cameras live in the SfM
    frame, so we undo the display normalization (``(p - offset) / scale``, exactly
    what the frontend sends to ``/project-point``) before projecting.
    """
    from app.services import sfm, registration, storage

    geom = a.get("geometry_json")
    insp_id = a.get("inspection_id")
    if not geom or len(geom) < 2 or not insp_id:
        return None

    c = cache.setdefault(insp_id, {})
    if "cameras" not in c:
        c["cameras"] = None
        models_dir = storage.get_models_dir(insp_id)
        sfm_files = list(models_dir.glob("**/cameras.sfm"))
        if sfm_files:
            parent = sfm_files[0].parent
            c["mesh"] = next((parent / f"model_{lod}.gltf" for lod in ("lod0", "lod1", "lod2")
                              if (parent / f"model_{lod}.gltf").exists()), None)
            c["cameras"] = sfm.load_cameras(sfm_files[0])
            try:
                c["scale"], c["offset"] = registration.display_transform(c["mesh"]) if c["mesh"] else (1.0, [0, 0, 0])
            except Exception:
                c["scale"], c["offset"] = 1.0, [0, 0, 0]
    cameras = c.get("cameras")
    if not cameras:
        return None
    mesh, s = c.get("mesh"), c["scale"]
    ox, oy, oz = c["offset"]

    # scene → mesh frame for every polygon vertex + the centroid
    vm = [(((p["x"] - ox) / s), ((p["y"] - oy) / s), ((p["z"] - oz) / s)) for p in geom]
    cx = sum(v[0] for v in vm) / len(vm)
    cy = sum(v[1] for v in vm) / len(vm)
    cz = sum(v[2] for v in vm) / len(vm)

    normal = None
    if mesh is not None:
        nm = sfm.surface_normal(mesh, cx, cy, cz)
        if nm:
            normal = sfm.mesh_to_sfm(*nm)

    matches = sfm.project_point(cameras, *sfm.mesh_to_sfm(cx, cy, cz), normal=normal)
    if not matches or mesh is None:
        return None
    # Keep ONLY photos that verifiably see the defect (ray from the camera through
    # the marker hits the mesh right at the point). No fallback: if nothing
    # verifies, the defect is occluded in every photo — drawing its polygon would
    # land it on some unrelated surface (e.g. the background), so we show nothing.
    try:
        matches = sfm.filter_by_backprojection(matches[:150], cameras, mesh, cx, cy, cz)
    except Exception:
        return None
    if not matches:
        return None

    outline_sfm = [sfm.mesh_to_sfm(*v) for v in vm]
    # the photo where the defect is large and wholly in frame, not merely the closest
    matches = sfm.rank_by_outline(cameras, matches[:80], outline_sfm)
    best = matches[0]["filename"]
    uv = sfm.project_outline(cameras, [best], outline_sfm).get(best)
    if not uv or all(p is None for p in uv):
        return None
    photo = storage.get_photos_dir(insp_id) / best
    if not photo.is_file():
        return None
    return photo, uv


def _outlined_defect_image(a: dict, cache: dict, max_w: float, max_h: float) -> Optional[Image]:
    """A source photo of the defect with its polygon boundary drawn on it."""
    res = _defect_outline_projection(a, cache)
    if not res:
        return None
    photo, uv = res
    try:
        from PIL import Image as PImage, ImageDraw, ImageOps
        im = ImageOps.exif_transpose(PImage.open(photo)).convert("RGB")
        im.thumbnail((1400, 1400), PImage.LANCZOS)
        w, h = im.size
        pts = [(p["u"] * w, p["v"] * h) for p in uv if p]
        if len(pts) >= 2:
            col = SEVERITY_COLORS.get(a.get("severity", "low"), RED)
            rgb = (int(col.red * 255), int(col.green * 255), int(col.blue * 255))
            overlay = PImage.new("RGBA", im.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            lw = max(3, w // 300)
            if len(pts) >= 3:
                od.polygon(pts, fill=rgb + (55,))
            od.line(pts + [pts[0]], fill=rgb + (255,), width=lw)
            for x, y in pts:                                    # vertex dots
                od.ellipse([x - lw, y - lw, x + lw, y + lw], fill=rgb + (255,))
            im = PImage.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
        bio = BytesIO()
        im.save(bio, format="JPEG", quality=85)
        bio.seek(0)
        ratio = min(max_w / w, max_h / h)
        return Image(bio, width=w * ratio, height=h * ratio)
    except Exception:
        return None


def _fit_image(path: Path, max_w: float, max_h: float) -> Optional[Image]:
    """Load an image as a flowable scaled to fit within max_w × max_h (aspect kept)."""
    try:
        iw, ih = ImageReader(str(path)).getSize()
        if not iw or not ih:
            return None
        ratio = min(max_w / iw, max_h / ih)
        return Image(str(path), width=iw * ratio, height=ih * ratio)
    except Exception:
        return None


def _thumb_image(path: Path, max_w: float, max_h: float, max_px: int = 1400) -> Optional[Image]:
    """Like _fit_image but re-encodes a downscaled JPEG so the PDF stays small
    even with many full-resolution photos (crucial for the AI overview report)."""
    try:
        from PIL import Image as PImage
        im = PImage.open(path).convert("RGB")
        im.thumbnail((max_px, max_px), PImage.LANCZOS)
        bio = BytesIO()
        im.save(bio, format="JPEG", quality=82)
        iw, ih = im.size
        ratio = min(max_w / iw, max_h / ih)
        bio.seek(0)
        return Image(bio, width=iw * ratio, height=ih * ratio)
    except Exception:
        return None


def _image_from_data_url(data: str, max_w: float, max_h: float) -> Optional[Image]:
    """Decode a base64 data-URL (or bare base64) into a fitted Image flowable."""
    import base64
    try:
        if not data:
            return None
        b64 = data.split(",", 1)[1] if data.startswith("data:") else data
        raw = base64.b64decode(b64)
        bio = BytesIO(raw)
        iw, ih = ImageReader(bio).getSize()
        if not iw or not ih:
            return None
        ratio = min(max_w / iw, max_h / ih)
        bio.seek(0)
        return Image(bio, width=iw * ratio, height=ih * ratio)
    except Exception:
        return None


def _severity_chart(by_severity: dict):
    """Horizontal bar chart of defect counts per severity (ReportLab graphics)."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    order = [("critical", "Kritická", RED), ("high", "Vysoká", ORANGE),
             ("medium", "Střední", YELLOW), ("low", "Nízká", GREEN)]
    counts = [by_severity.get(k, 0) for k, _, _ in order]
    mx = max(counts) or 1
    row_h, gap, top, left, bar_max = 20, 8, 10, 95, 320
    d = Drawing(460, top + len(order) * (row_h + gap))
    for i, (key, label, col) in enumerate(order):
        y = d.height - top - (i + 1) * row_h - i * gap
        d.add(String(0, y + row_h / 2 - 4, label, fontName=FONT, fontSize=9, fillColor=BLACK))
        n = by_severity.get(key, 0)
        w = max(2, (n / mx) * bar_max)
        d.add(Rect(left, y, w, row_h, fillColor=col, strokeColor=None))
        d.add(String(left + w + 6, y + row_h / 2 - 4, str(n), fontName=FONT_BOLD, fontSize=9, fillColor=BLACK))
    return d

# ─── Color palette ───────────────────────────────────────────────────────────

BLUE = colors.HexColor("#1e40af")
BLUE_LIGHT = colors.HexColor("#eff6ff")
GRAY = colors.HexColor("#6b7280")
GRAY_LIGHT = colors.HexColor("#f3f4f6")
RED = colors.HexColor("#dc2626")
ORANGE = colors.HexColor("#ea580c")
YELLOW = colors.HexColor("#ca8a04")
GREEN = colors.HexColor("#16a34a")
BLACK = colors.HexColor("#111827")
WHITE = colors.white

SEVERITY_COLORS = {
    "low": GREEN,
    "medium": YELLOW,
    "high": ORANGE,
    "critical": RED,
}

SEVERITY_LABELS = {
    "low": "Nízká",
    "medium": "Střední",
    "high": "Vysoká",
    "critical": "Kritická",
}

DEFECT_LABELS = {
    "crack": "Prasklina",
    "spalling": "Opadání betonu",
    "stain": "Skvrna/výkvět",
    "moss": "Mech / vegetace",
    "corrosion": "Koroze / rez",
    "deformation": "Deformace",
    "missing_material": "Chybějící materiál",
    "water_damage": "Poškození vodou",
    "efflorescence": "Výkvět solí",
    "void": "Dutina / hloubková trhlina",
    "other": "Jiné",
}

METHOD_LABELS = {
    "drone": "Dron",
    "mobile": "Mobilní zařízení",
    "manual": "Manuální",
    "combined": "Kombinovaná",
}

DEFECT_STATUS_LABELS = {
    "new": "Nová",
    "monitoring": "Sledovaná",
    "repaired": "Opravená",
    "dismissed": "Zamítnutá",
}


def _get_styles():
    styles = getSampleStyleSheet()
    # swap every built-in style onto the Unicode font (bold styles → bold TTF)
    for s in styles.byName.values():
        fn = getattr(s, "fontName", None)
        if fn is not None:
            s.fontName = FONT_BOLD if "Bold" in fn else FONT
    styles.add(ParagraphStyle("Title2", parent=styles["Title"], textColor=BLUE, fontSize=22, spaceAfter=6, fontName=FONT_BOLD))
    styles.add(ParagraphStyle("Heading2b", parent=styles["Heading2"], textColor=BLUE, fontSize=13, spaceBefore=12, fontName=FONT_BOLD))
    styles.add(ParagraphStyle("SmallGray", parent=styles["Normal"], textColor=GRAY, fontSize=8))
    styles.add(ParagraphStyle("CellText", parent=styles["Normal"], fontSize=9))
    styles.add(ParagraphStyle("CellBold", parent=styles["Normal"], fontSize=9, fontName=FONT_BOLD))
    return styles


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BLUE)
    canvas.rect(0, A4[1] - 1.2 * cm, A4[0], 1.2 * cm, fill=True, stroke=False)
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_BOLD, 10)
    canvas.drawString(1.5 * cm, A4[1] - 0.85 * cm, "VISTA — Inspekční zpráva")
    canvas.setFillColor(GRAY)
    canvas.setFont(FONT, 8)
    canvas.drawRightString(A4[0] - 1.5 * cm, 0.6 * cm, f"Strana {doc.page}")
    canvas.drawString(1.5 * cm, 0.6 * cm, f"Vygenerováno: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    canvas.setStrokeColor(GRAY_LIGHT)
    canvas.line(1.5 * cm, 0.9 * cm, A4[0] - 1.5 * cm, 0.9 * cm)
    canvas.restoreState()


def generate_inspection_report(
    structure: dict,
    inspection: dict,
    annotations: list[dict],
    measurements: list[dict],
    photo_set: Optional[dict] = None,
    options: Optional[dict] = None,
) -> bytes:
    opts = options or {}
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        topMargin=2.0 * cm,
        bottomMargin=1.8 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    template = PageTemplate("main", frames=[frame], onPage=_header_footer)
    doc.addPageTemplates([template])

    styles = _get_styles()
    story = []

    # ─── Cover ───────────────────────────────────────────────────────────────
    # Optional company logo + name band at the very top.
    logo_img = _image_from_data_url(opts.get("logo", ""), max_w=5 * cm, max_h=2.2 * cm)
    company = opts.get("company_name")
    if logo_img or company:
        brand_cell = logo_img or Paragraph(f"<b>{company}</b>", styles["Heading2b"])
        name_cell = Paragraph(company or "", styles["Normal"]) if logo_img else Paragraph("", styles["Normal"])
        band = Table([[brand_cell, name_cell]], colWidths=[6 * cm, 10 * cm])
        band.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(band)
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1, spaceAfter=6))

    story.append(Spacer(1, 1.0 * cm))
    story.append(Paragraph("INSPEKČNÍ ZPRÁVA", styles["Title2"]))
    story.append(Paragraph(structure["name"], styles["Heading1"]))
    if opts.get("protocol_number"):
        story.append(Paragraph(f"Protokol č. {opts['protocol_number']}", styles["Normal"]))
    story.append(HRFlowable(width="100%", color=BLUE, thickness=2, spaceAfter=8))

    cover_data = [
        ["Typ konstrukce:", _structure_type_label(structure.get("type", ""))],
        ["Lokalita:", structure.get("location_name") or "—"],
        ["Rok výstavby:", str(structure.get("year_built")) if structure.get("year_built") else "—"],
        ["Materiál:", structure.get("material") or "—"],
        ["Název inspekce:", inspection["name"]],
        ["Datum inspekce:", _fmt_date(inspection.get("inspection_date", ""))],
        ["Metoda sběru dat:", METHOD_LABELS.get(inspection.get("method", ""), inspection.get("method") or "—")],
        ["Inspektor:", inspection.get("inspector_name") or "—"],
        ["Počasí:", inspection.get("weather") or "—"],
        ["Stav inspekce:", _status_label(inspection.get("status", ""))],
    ]
    if opts.get("protocol_number"):
        cover_data.insert(0, ["Číslo protokolu:", str(opts["protocol_number"])])
    t = Table(cover_data, colWidths=[5 * cm, 11 * cm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("TEXTCOLOR", (1, 0), (1, -1), BLACK),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, GRAY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)

    if inspection.get("description"):
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph("<b>Poznámky k inspekci:</b>", styles["Normal"]))
        story.append(Paragraph(inspection["description"], styles["Normal"]))

    story.append(PageBreak())

    # ─── Summary statistics ───────────────────────────────────────────────────
    story.append(Paragraph("Souhrn nálezů", styles["Heading2b"]))

    by_severity = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for a in annotations:
        by_severity[a.get("severity", "low")] = by_severity.get(a.get("severity", "low"), 0) + 1

    stat_data = [
        ["Celkem poruch", str(len(annotations))],
        ["Kritické", str(by_severity["critical"])],
        ["Vysoká závažnost", str(by_severity["high"])],
        ["Střední závažnost", str(by_severity["medium"])],
        ["Nízká závažnost", str(by_severity["low"])],
        ["Měření", str(len(measurements))],
    ]
    stat_colors = [GRAY_LIGHT, colors.HexColor("#fee2e2"), colors.HexColor("#fed7aa"),
                   colors.HexColor("#fef9c3"), colors.HexColor("#dcfce7"), GRAY_LIGHT]
    st = Table(stat_data, colWidths=[8 * cm, 8 * cm])
    st.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTNAME", (1, 0), (1, -1), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        *[("BACKGROUND", (0, i), (-1, i), c) for i, c in enumerate(stat_colors)],
    ]))
    story.append(st)

    # Severity distribution chart
    if annotations:
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph("Rozložení podle závažnosti", styles["Heading2b"]))
        story.append(Spacer(1, 0.2 * cm))
        story.append(_severity_chart(by_severity))

    # ─── 3D snapshot with numbered defect markers (captured on the client) ─────
    snap = _image_from_data_url(opts.get("scene_snapshot", ""), max_w=doc.width, max_h=11 * cm)
    if snap is not None:
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph("Mapa poruch na modelu", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.3 * cm))
        story.append(snap)
        story.append(Paragraph(
            "Čísla značek odpovídají pořadí poruch v tabulce níže.",
            styles["Normal"]))

    # ─── Annotations ─────────────────────────────────────────────────────────
    if annotations and opts.get("include_annotations", True):
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph("Nalezené poruchy", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.3 * cm))

        ann_header = [
            Paragraph("<b>#</b>", styles["CellBold"]),
            Paragraph("<b>Typ poruchy</b>", styles["CellBold"]),
            Paragraph("<b>Závažnost</b>", styles["CellBold"]),
            Paragraph("<b>Stav</b>", styles["CellBold"]),
            Paragraph("<b>Popis</b>", styles["CellBold"]),
            Paragraph("<b>Rozměry</b>", styles["CellBold"]),
        ]
        ann_rows = [ann_header]
        for i, a in enumerate(annotations):
            sev = a.get("severity", "low")
            stat = a.get("status") or "new"
            dims = []
            if a.get("length_m"):
                dims.append(f"D: {a['length_m']:.2f} m")
            if a.get("area_m2"):
                dims.append(f"P: {a['area_m2']:.2f} m²")
            ann_rows.append([
                Paragraph(str(i + 1), styles["CellText"]),
                Paragraph(DEFECT_LABELS.get(a.get("defect_type", ""), a.get("defect_type", "")), styles["CellText"]),
                Paragraph(SEVERITY_LABELS.get(sev, sev), styles["CellText"]),
                Paragraph(DEFECT_STATUS_LABELS.get(stat, stat), styles["CellText"]),
                Paragraph(a.get("description") or "—", styles["CellText"]),
                Paragraph(", ".join(dims) or "—", styles["CellText"]),
            ])

        at = Table(ann_rows, colWidths=[0.9 * cm, 3.0 * cm, 2.1 * cm, 2.0 * cm, 6.2 * cm, 2.3 * cm])
        bg_colors_ann = []
        for i, a in enumerate(annotations):
            sev = a.get("severity", "low")
            c = {
                "critical": colors.HexColor("#fee2e2"),
                "high": colors.HexColor("#fed7aa"),
                "medium": colors.HexColor("#fef9c3"),
                "low": colors.HexColor("#dcfce7"),
            }.get(sev, WHITE)
            bg_colors_ann.append(("BACKGROUND", (0, i + 1), (-1, i + 1), c))

        at.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            *bg_colors_ann,
        ]))
        story.append(at)

        # ─── Representative photo per defect ─────────────────────────────────
        photo_cards = []
        _proj_cache: dict = {}          # cameras / display-transform per inspection
        for i, a in enumerate(annotations):
            # Prefer a source photo with the defect POLYGON drawn on it; only fall
            # back to a plain photo when the user hand-picked one or projection
            # fails (no cameras / defect not visible in any reconstructed photo).
            img = None
            outlined = False
            if not a.get("report_photo"):
                img = _outlined_defect_image(a, _proj_cache, max_w=6 * cm, max_h=5 * cm)
                outlined = img is not None
            if img is None:
                img_path = _report_photo_path(a)
                if not img_path:
                    continue
                img = _fit_image(img_path, max_w=6 * cm, max_h=5 * cm)
            if img is None:
                continue
            sev = a.get("severity", "low")
            head = (
                f"<b>#{i + 1} — {DEFECT_LABELS.get(a.get('defect_type', ''), a.get('defect_type', ''))}</b>"
                f"  ·  {SEVERITY_LABELS.get(sev, sev)}"
            )
            lines = [Paragraph(head, styles["CellText"])]
            if a.get("description"):
                lines.append(Spacer(1, 0.15 * cm))
                lines.append(Paragraph(a["description"], styles["CellText"]))
            dims = []
            if a.get("length_m"):
                dims.append(f"Délka: {a['length_m']:.2f} m")
            if a.get("area_m2"):
                dims.append(f"Plocha: {a['area_m2']:.2f} m²")
            if dims:
                lines.append(Spacer(1, 0.15 * cm))
                lines.append(Paragraph(", ".join(dims), styles["CellText"]))
            if outlined:
                lines.append(Spacer(1, 0.15 * cm))
                lines.append(Paragraph(
                    "<i>Obrys poruchy vyznačen ve snímku.</i>", styles["CellText"]))
            card = Table([[img, lines]], colWidths=[6.4 * cm, 10.6 * cm])
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            photo_cards.append(KeepTogether([card, Spacer(1, 0.3 * cm)]))

        if photo_cards:
            story.append(Spacer(1, 0.6 * cm))
            story.append(Paragraph("Fotodokumentace poruch", styles["Heading2b"]))
            story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
            story.append(Spacer(1, 0.3 * cm))
            story.extend(photo_cards)

        # ─── Remediation plan (only defects with an action / owner / cost) ────
        rem = [(i, a) for i, a in enumerate(annotations)
               if a.get("recommended_action") or a.get("responsible") or a.get("due_date") or a.get("cost_estimate")]
        if rem:
            story.append(Spacer(1, 0.6 * cm))
            story.append(Paragraph("Plán oprav a doporučená opatření", styles["Heading2b"]))
            story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
            story.append(Spacer(1, 0.3 * cm))
            rem_rows = [[
                Paragraph("<b>#</b>", styles["CellBold"]),
                Paragraph("<b>Doporučené opatření</b>", styles["CellBold"]),
                Paragraph("<b>Odpovědný</b>", styles["CellBold"]),
                Paragraph("<b>Termín</b>", styles["CellBold"]),
                Paragraph("<b>Náklady</b>", styles["CellBold"]),
            ]]
            total_cost = 0.0
            for i, a in rem:
                cost = a.get("cost_estimate")
                if cost:
                    total_cost += cost
                cost_txt = f"{cost:,.0f} Kč".replace(",", " ") if cost else "—"
                rem_rows.append([
                    Paragraph(str(i + 1), styles["CellText"]),
                    Paragraph(a.get("recommended_action") or "—", styles["CellText"]),
                    Paragraph(a.get("responsible") or "—", styles["CellText"]),
                    Paragraph(a.get("due_date") or "—", styles["CellText"]),
                    Paragraph(cost_txt, styles["CellText"]),
                ])
            if total_cost:
                rem_rows.append([
                    Paragraph("", styles["CellText"]),
                    Paragraph("<b>Celkem odhad nákladů</b>", styles["CellBold"]),
                    Paragraph("", styles["CellText"]),
                    Paragraph("", styles["CellText"]),
                    Paragraph(f"<b>{total_cost:,.0f} Kč</b>".replace(",", " "), styles["CellBold"]),
                ])
            rt = Table(rem_rows, colWidths=[0.9 * cm, 8.1 * cm, 3.0 * cm, 2.3 * cm, 2.2 * cm])
            rt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BLUE),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(rt)

        # ─── 3D view per defect (face-on close-ups, so ALL defects are shown) ──
        snaps = opts.get("defect_snapshots") or {}
        views = []
        for i, a in enumerate(annotations):
            data = snaps.get(a.get("id"))
            img = _image_from_data_url(data, max_w=8 * cm, max_h=6 * cm) if data else None
            if img is None:
                continue
            sev = a.get("severity", "low")
            head = (f"<b>#{i + 1} — {DEFECT_LABELS.get(a.get('defect_type', ''), a.get('defect_type', ''))}</b>"
                    f"  ·  {SEVERITY_LABELS.get(sev, sev)}")
            lines = [Paragraph(head, styles["CellText"])]
            if a.get("description"):
                lines.append(Spacer(1, 0.15 * cm))
                lines.append(Paragraph(a["description"], styles["CellText"]))
            card = Table([[img, lines]], colWidths=[8.3 * cm, 8.7 * cm])
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            views.append(KeepTogether([card, Spacer(1, 0.3 * cm)]))
        if views:
            story.append(Spacer(1, 0.6 * cm))
            story.append(Paragraph("3D pohledy na poruchy", styles["Heading2b"]))
            story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
            story.append(Spacer(1, 0.3 * cm))
            story.extend(views)

    # ─── Measurements ────────────────────────────────────────────────────────
    if measurements and opts.get("include_measurements", True):
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph("Měření", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.3 * cm))

        m_header = [
            Paragraph("<b>#</b>", styles["CellBold"]),
            Paragraph("<b>Název</b>", styles["CellBold"]),
            Paragraph("<b>Typ</b>", styles["CellBold"]),
            Paragraph("<b>Hodnota</b>", styles["CellBold"]),
            Paragraph("<b>Poznámky</b>", styles["CellBold"]),
        ]
        m_rows = [m_header]
        type_labels = {"distance": "Vzdálenost", "area": "Plocha", "volume": "Objem", "angle": "Úhel", "photo": "Z fotky"}
        scale = inspection.get("scale_factor") or 1.0
        for i, m in enumerate(measurements):
            if m.get("value") is None:
                val = "—"
            elif m.get("measurement_type") == "photo":
                # in-photo measurements are absolute (calibrated against a reference)
                val = f"{m['value']:.2f} {m.get('unit', 'mm')}"
            else:
                val = f"{m['value'] * scale:.3f} {m.get('unit', 'm')}"
            m_rows.append([
                Paragraph(str(i + 1), styles["CellText"]),
                Paragraph(m.get("name") or f"Měření {i+1}", styles["CellText"]),
                Paragraph(type_labels.get(m.get("measurement_type", ""), m.get("measurement_type", "")), styles["CellText"]),
                Paragraph(val, styles["CellText"]),
                Paragraph(m.get("notes") or "—", styles["CellText"]),
            ])

        mt = Table(m_rows, colWidths=[1 * cm, 4 * cm, 3 * cm, 3 * cm, 5.5 * cm])
        mt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(mt)

    # ─── Notes ───────────────────────────────────────────────────────────────
    if opts.get("notes"):
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph("Závěrečné poznámky", styles["Heading2b"]))
        story.append(Paragraph(opts["notes"], styles["Normal"]))

    # ─── Signature block ─────────────────────────────────────────────────────
    story.append(Spacer(1, 1.5 * cm))
    story.append(HRFlowable(width="100%", color=GRAY_LIGHT, thickness=1))
    sig_data = [
        [f"Zpracoval: {opts.get('prepared_by') or inspection.get('inspector_name') or '___________________'}",
         f"Datum: {_fmt_date(inspection.get('inspection_date', ''))}"],
        ["Podpis: ___________________", f"Razítko{(' — ' + opts['company_name']) if opts.get('company_name') else ''}:"],
    ]
    st2 = Table(sig_data, colWidths=[9 * cm, 7.5 * cm])
    st2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(st2)

    doc.build(story)
    buf.seek(0)
    return buf.read()


def generate_structure_report(
    structure: dict,
    inspections: list[dict],
    all_annotations: list[dict],
    all_measurements: list[dict],
    options: Optional[dict] = None,
) -> bytes:
    """Summary report across all inspections of a structure."""
    opts = options or {}
    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, topMargin=2.0*cm, bottomMargin=1.8*cm,
                          leftMargin=1.5*cm, rightMargin=1.5*cm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate("main", frames=[frame], onPage=_header_footer)])

    styles = _get_styles()
    story = []

    story.append(Spacer(1, 1.5 * cm))
    story.append(Paragraph("KATALOG KONSTRUKCE", styles["Title2"]))
    story.append(Paragraph(structure["name"], styles["Heading1"]))
    story.append(HRFlowable(width="100%", color=BLUE, thickness=2, spaceAfter=8))

    # Structure info
    info_data = [
        ["Typ:", _structure_type_label(structure.get("type", ""))],
        ["Lokalita:", structure.get("location_name") or "—"],
        ["Rok výstavby:", str(structure.get("year_built")) if structure.get("year_built") else "—"],
        ["Materiál:", structure.get("material") or "—"],
        ["Popis:", structure.get("description") or "—"],
        ["Celkem inspekcí:", str(len(inspections))],
        ["Celkem poruch:", str(len(all_annotations))],
        ["Celkem měření:", str(len(all_measurements))],
    ]
    t = Table(info_data, colWidths=[4*cm, 12*cm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, GRAY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)

    # Inspection history
    if inspections:
        story.append(Spacer(1, 0.8*cm))
        story.append(Paragraph("Historie inspekcí", styles["Heading2b"]))
        h_rows = [[
            Paragraph("<b>Datum</b>", styles["CellBold"]),
            Paragraph("<b>Název</b>", styles["CellBold"]),
            Paragraph("<b>Metoda</b>", styles["CellBold"]),
            Paragraph("<b>Inspektor</b>", styles["CellBold"]),
            Paragraph("<b>Poruchy</b>", styles["CellBold"]),
            Paragraph("<b>Stav</b>", styles["CellBold"]),
        ]]
        for insp in sorted(inspections, key=lambda x: x.get("inspection_date", ""), reverse=True):
            n_ann = sum(1 for a in all_annotations if a.get("inspection_id") == insp["id"])
            h_rows.append([
                Paragraph(_fmt_date(insp.get("inspection_date", "")), styles["CellText"]),
                Paragraph(insp.get("name", ""), styles["CellText"]),
                Paragraph(METHOD_LABELS.get(insp.get("method", ""), insp.get("method") or "—"), styles["CellText"]),
                Paragraph(insp.get("inspector_name") or "—", styles["CellText"]),
                Paragraph(str(n_ann), styles["CellText"]),
                Paragraph(_status_label(insp.get("status", "")), styles["CellText"]),
            ])
        ht = Table(h_rows, colWidths=[2.5*cm, 4*cm, 2.5*cm, 3*cm, 2*cm, 2.5*cm])
        ht.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(ht)

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_date(d) -> str:
    if not d:
        return "—"
    try:
        if hasattr(d, "strftime"):
            return d.strftime("%d.%m.%Y")
        from datetime import date as date_type
        parsed = datetime.fromisoformat(str(d))
        return parsed.strftime("%d.%m.%Y")
    except Exception:
        return str(d)


def _structure_type_label(t: str) -> str:
    labels = {
        "building": "Budova",
        "bridge": "Most",
        "tower": "Stožár / věž",
        "chimney": "Komín",
        "mast": "Stožár",
        "dam": "Přehrada / hráz",
        "tunnel": "Tunel",
        "other": "Jiné",
    }
    return labels.get(t, t)


def _status_label(s: str) -> str:
    labels = {
        "draft": "Návrh",
        "in_progress": "Probíhá",
        "completed": "Dokončeno",
        "archived": "Archivováno",
    }
    return labels.get(s, s)


def generate_ai_segmentation_report(
    structure: dict,
    inspection: dict,
    results: dict,
    class_labels: dict,
    damage_classes: list[str],
    options: Optional[dict] = None,
) -> bytes:
    """Overview of AI defect segmentation: per-image overlay + %-coverage table."""
    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, topMargin=2.0 * cm, bottomMargin=1.8 * cm,
                          leftMargin=1.5 * cm, rightMargin=1.5 * cm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate("main", frames=[frame], onPage=_header_footer)])
    styles = _get_styles()
    story = []

    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("AI ANALÝZA PORUCH", styles["Title2"]))
    story.append(Paragraph(structure.get("name", ""), styles["Heading1"]))
    story.append(Paragraph(
        f"{inspection.get('name', '')} · model dacl10k (19 tříd)", styles["Normal"]))
    story.append(HRFlowable(width="100%", color=BLUE, thickness=2, spaceAfter=8))
    story.append(Paragraph(
        "Automatická segmentace — orientační podklad k odborné revizi, nikoli verdikt. "
        "Trhliny a kaverny model rozpoznává slabě.", styles["Normal"]))

    images = results.get("images", [])
    insp_id = inspection.get("id")
    seg_dir = settings.upload_path / "inspections" / str(insp_id) / "ai_segmentation" if insp_id else None

    # Aggregate coverage across all images (mean % per damage class present).
    agg: dict[str, float] = {}
    for im in images:
        for c, v in (im.get("coverage") or {}).items():
            if c in damage_classes and v:
                agg[c] = agg.get(c, 0.0) + v
    if agg:
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph("Souhrn detekovaných poruch", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.2 * cm))
        rows = [[Paragraph("<b>Třída poruchy</b>", styles["CellBold"]),
                 Paragraph("<b>Snímků s výskytem</b>", styles["CellBold"]),
                 Paragraph("<b>Průměrné pokrytí</b>", styles["CellBold"])]]
        for c in sorted(agg, key=lambda k: -agg[k]):
            n = sum(1 for im in images if (im.get("coverage") or {}).get(c, 0) > 0.001)
            rows.append([
                Paragraph(class_labels.get(c, c), styles["CellText"]),
                Paragraph(str(n), styles["CellText"]),
                Paragraph(f"{agg[c] / max(1, len(images)) * 100:.2f} %", styles["CellText"]),
            ])
        t = Table(rows, colWidths=[8 * cm, 4.5 * cm, 4 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)

    # Per-image cards — only images where a damage class was detected.
    flagged = [im for im in images
               if any((im.get("coverage") or {}).get(c, 0) > 0.001 for c in damage_classes)]
    if flagged:
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph(f"Snímky s nálezem ({len(flagged)} z {len(images)})", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.3 * cm))
        for im in flagged:
            img_flow = None
            if seg_dir and im.get("overlay"):
                p = seg_dir / im["overlay"]
                if p.is_file():
                    img_flow = _thumb_image(p, max_w=7 * cm, max_h=5.5 * cm)
            found = sorted(
                ((c, v) for c, v in (im.get("coverage") or {}).items()
                 if c in damage_classes and v > 0.001),
                key=lambda kv: -kv[1],
            )
            lines = [Paragraph(f"<b>{im.get('filename', '')}</b>", styles["CellText"])]
            for c, v in found:
                lines.append(Paragraph(f"{class_labels.get(c, c)}: {v * 100:.2f} %", styles["CellText"]))
            card = Table([[img_flow or Paragraph("—", styles["CellText"]), lines]],
                         colWidths=[7.3 * cm, 9.7 * cm])
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(KeepTogether([card, Spacer(1, 0.3 * cm)]))
    else:
        story.append(Spacer(1, 0.5 * cm))
        story.append(Paragraph("Na žádném snímku nebyla detekována porucha nad prahem.", styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _sev_counts(annotations: list[dict]) -> dict:
    out = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for a in annotations:
        out[a.get("severity", "low")] = out.get(a.get("severity", "low"), 0) + 1
    return out


def generate_comparison_report(
    structure: dict,
    inspection_a: dict,
    inspection_b: dict,
    annotations_a: list[dict],
    annotations_b: list[dict],
    options: Optional[dict] = None,
) -> bytes:
    """Side-by-side degradation report between two inspections (A = starší, B = novější)."""
    opts = options or {}
    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, topMargin=2.0 * cm, bottomMargin=1.8 * cm,
                          leftMargin=1.5 * cm, rightMargin=1.5 * cm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate("main", frames=[frame], onPage=_header_footer)])
    styles = _get_styles()
    story = []

    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph("POROVNÁNÍ INSPEKCÍ", styles["Title2"]))
    story.append(Paragraph(structure["name"], styles["Heading1"]))
    story.append(HRFlowable(width="100%", color=BLUE, thickness=2, spaceAfter=8))

    name_a = f"{inspection_a.get('name', 'A')} ({_fmt_date(inspection_a.get('inspection_date', ''))})"
    name_b = f"{inspection_b.get('name', 'B')} ({_fmt_date(inspection_b.get('inspection_date', ''))})"

    ca, cb = _sev_counts(annotations_a), _sev_counts(annotations_b)
    cost_a = sum(a.get("cost_estimate") or 0 for a in annotations_a)
    cost_b = sum(a.get("cost_estimate") or 0 for a in annotations_b)

    def _delta(x_new, x_old):
        d = x_new - x_old
        if d == 0:
            return "beze změny"
        return f"+{d}" if d > 0 else str(d)

    rows = [
        [Paragraph("<b>Ukazatel</b>", styles["CellBold"]),
         Paragraph(f"<b>A — {name_a}</b>", styles["CellBold"]),
         Paragraph(f"<b>B — {name_b}</b>", styles["CellBold"]),
         Paragraph("<b>Změna</b>", styles["CellBold"])],
    ]
    metrics = [
        ("Celkem poruch", len(annotations_a), len(annotations_b)),
        ("Kritické", ca["critical"], cb["critical"]),
        ("Vysoká závažnost", ca["high"], cb["high"]),
        ("Střední závažnost", ca["medium"], cb["medium"]),
        ("Nízká závažnost", ca["low"], cb["low"]),
    ]
    for label, va, vb in metrics:
        rows.append([
            Paragraph(label, styles["CellText"]),
            Paragraph(str(va), styles["CellText"]),
            Paragraph(str(vb), styles["CellText"]),
            Paragraph(_delta(vb, va), styles["CellText"]),
        ])
    rows.append([
        Paragraph("Odhad nákladů", styles["CellText"]),
        Paragraph(f"{cost_a:,.0f} Kč".replace(",", " "), styles["CellText"]),
        Paragraph(f"{cost_b:,.0f} Kč".replace(",", " "), styles["CellText"]),
        Paragraph(_delta(int(cost_b), int(cost_a)) + " Kč", styles["CellText"]),
    ])

    ct = Table(rows, colWidths=[4.5 * cm, 4.5 * cm, 4.5 * cm, 3.0 * cm])
    ct.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GRAY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(ct)

    # Overall trend note
    dtot = len(annotations_b) - len(annotations_a)
    dcrit = cb["critical"] - ca["critical"]
    if dtot > 0 or dcrit > 0:
        trend = f"Stav se zhoršil: přibylo {dtot} poruch" + (f", z toho kritických +{dcrit}." if dcrit > 0 else ".")
        tcol = colors.HexColor("#fee2e2")
    elif dtot < 0:
        trend = f"Stav se zlepšil: ubylo {abs(dtot)} poruch."
        tcol = colors.HexColor("#dcfce7")
    else:
        trend = "Počet poruch se nezměnil."
        tcol = GRAY_LIGHT
    story.append(Spacer(1, 0.4 * cm))
    note = Table([[Paragraph(f"<b>Trend:</b> {trend}", styles["Normal"])]], colWidths=[doc.width])
    note.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), tcol),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(note)

    # Side-by-side visual snapshots (captured on the client)
    snap_a = _image_from_data_url(opts.get("snapshot_a", ""), max_w=8.1 * cm, max_h=8 * cm)
    snap_b = _image_from_data_url(opts.get("snapshot_b", ""), max_w=8.1 * cm, max_h=8 * cm)
    if snap_a is not None or snap_b is not None:
        story.append(Spacer(1, 0.6 * cm))
        story.append(Paragraph("Vizuální porovnání", styles["Heading2b"]))
        story.append(HRFlowable(width="100%", color=BLUE_LIGHT, thickness=1))
        story.append(Spacer(1, 0.3 * cm))
        vis = Table([
            [Paragraph(f"<b>A</b> — {name_a}", styles["CellText"]),
             Paragraph(f"<b>B</b> — {name_b}", styles["CellText"])],
            [snap_a or Paragraph("—", styles["CellText"]),
             snap_b or Paragraph("—", styles["CellText"])],
        ], colWidths=[8.3 * cm, 8.3 * cm])
        vis.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(vis)

    doc.build(story)
    buf.seek(0)
    return buf.read()
