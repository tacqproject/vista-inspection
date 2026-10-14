"""
Compare defect annotations between two inspections of the same structure.

Each reconstruction lives in its own arbitrary coordinate frame (scale,
origin and tilt differ between SfM runs), so raw positions are not
comparable. We normalize each inspection's annotation cloud (centroid +
RMS radius) and match same-type defects by nearest normalized distance.
The matching is a heuristic to assist the inspector, not ground truth —
the threshold is deliberately generous and ties are resolved greedily
by ascending distance.
"""

import math

# max normalized distance (fraction of cloud RMS radius) to accept a match
MATCH_THRESHOLD = 0.6

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _normalize(annotations: list[dict]) -> dict[str, tuple[float, float, float]]:
    """Positions → centroid-centered, RMS-scaled coordinates (per inspection)."""
    pts = {
        a["id"]: (a["position_x"], a["position_y"], a["position_z"])
        for a in annotations
        if a.get("position_x") is not None
    }
    if not pts:
        return {}
    n = len(pts)
    cx = sum(p[0] for p in pts.values()) / n
    cy = sum(p[1] for p in pts.values()) / n
    cz = sum(p[2] for p in pts.values()) / n
    rms = math.sqrt(sum(
        (p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2
        for p in pts.values()
    ) / n) or 1.0
    return {
        k: ((p[0] - cx) / rms, (p[1] - cy) / rms, (p[2] - cz) / rms)
        for k, p in pts.items()
    }


def compare_annotations(
    old: list[dict],
    new: list[dict],
    matrix: list[float] | None = None,
    match_threshold: float | None = None,
    covered_ids: set | None = None,
) -> dict:
    """
    Pair annotations of the old (A) and new (B) inspection.
    With `matrix` (B registered onto A — see services/registration.py) the
    defects are matched by REAL distance in A's frame; without it we fall back
    to the per-cloud normalization heuristic, which is all that's possible when
    the two reconstructions live in unrelated frames.

    `covered_ids` (aligned mode) is the set of A-defect ids that lie inside the
    area B actually scanned. B is often only a PART of the structure, so an A
    defect with no match is only "resolved" if B looked there — otherwise it's
    "uncovered" (B didn't inspect that spot), not repaired.

    Returns matched pairs with a status and the leftovers:
      worse / improved / unchanged  — matched, severity delta
      new       — only in B (new defect)
      resolved  — only in A, inside B's coverage (possibly repaired)
      uncovered — only in A, outside B's coverage (not inspected by B)
    """
    if matrix:
        return _compare_aligned(old, new, matrix, match_threshold or 0.6, covered_ids)

    na, nb = _normalize(old), _normalize(new)

    # Same-type defects always pair up greedily (as many pairs as both sides
    # allow); normalized distance only orders the greedy choice and is
    # reported as confidence. Hard thresholds misfire on small sets, and a
    # defect shown as simultaneously "new" and "resolved" confuses more than
    # an uncertain pair the inspector can verify visually.
    candidates = []
    for a in old:
        for b in new:
            if a["defect_type"] != b["defect_type"]:
                continue
            pa, pb = na.get(a["id"]), nb.get(b["id"])
            d = math.dist(pa, pb) if pa and pb else MATCH_THRESHOLD
            candidates.append((d, a["id"], b["id"]))

    candidates.sort()
    used_a: set = set()
    used_b: set = set()
    pairs = []
    for d, aid, bid in candidates:
        if aid in used_a or bid in used_b:
            continue
        used_a.add(aid)
        used_b.add(bid)
        a = next(x for x in old if x["id"] == aid)
        b = next(x for x in new if x["id"] == bid)
        delta = SEVERITY_RANK.get(b["severity"], 0) - SEVERITY_RANK.get(a["severity"], 0)
        pairs.append({
            "old": a,
            "new": b,
            "match_distance": round(d, 3),
            "status": "worse" if delta > 0 else "improved" if delta < 0 else "unchanged",
            "severity_delta": delta,
        })

    only_new = [b for b in new if b["id"] not in used_b]
    unmatched_a = [a for a in old if a["id"] not in used_a]

    # An unmatched A defect counts as "resolved" only if B actually covered that
    # spot; otherwise B simply didn't inspect there → "uncovered". Coverage is
    # known only in aligned mode; without it (covered_ids is None) keep the old
    # behaviour and treat every unmatched A defect as potentially resolved.
    if covered_ids is None:
        only_resolved, uncovered = unmatched_a, []
    else:
        only_resolved = [a for a in unmatched_a if a["id"] in covered_ids]
        uncovered = [a for a in unmatched_a if a["id"] not in covered_ids]

    return {
        "pairs": pairs,
        "new": only_new,
        "resolved": only_resolved,
        "uncovered": uncovered,
        "summary": {
            "matched": len(pairs),
            "worse": sum(1 for p in pairs if p["status"] == "worse"),
            "improved": sum(1 for p in pairs if p["status"] == "improved"),
            "unchanged": sum(1 for p in pairs if p["status"] == "unchanged"),
            "new": len(only_new),
            "resolved": len(only_resolved),
            "uncovered": len(uncovered),
        },
    }


# ─── Aligned comparison (registered models) ──────────────────────────────────

# defects farther apart than this (in A's mesh units, converted from the
# registration's metric residual) are different defects
ALIGNED_MATCH_M = 0.6


def _apply(matrix: list[float], p: tuple[float, float, float]) -> tuple[float, float, float]:
    m = matrix
    x, y, z = p
    w = m[12] * x + m[13] * y + m[14] * z + m[15] or 1.0
    return (
        (m[0] * x + m[1] * y + m[2] * z + m[3]) / w,
        (m[4] * x + m[5] * y + m[6] * z + m[7]) / w,
        (m[8] * x + m[9] * y + m[10] * z + m[11]) / w,
    )


def _compare_aligned(
    old: list[dict], new: list[dict], matrix: list[float], thresh: float,
    covered_ids: set | None = None,
) -> dict:
    """Match by true distance: B's positions are mapped into A's frame first."""

    candidates = []
    for a in old:
        if a.get("position_x") is None:
            continue
        pa = (a["position_x"], a["position_y"], a["position_z"])
        for b in new:
            if b.get("position_x") is None or a["defect_type"] != b["defect_type"]:
                continue
            pb = _apply(matrix, (b["position_x"], b["position_y"], b["position_z"]))
            d = math.dist(pa, pb)
            if d <= thresh:
                candidates.append((d, a["id"], b["id"]))

    candidates.sort()
    used_a: set = set()
    used_b: set = set()
    pairs = []
    for d, aid, bid in candidates:
        if aid in used_a or bid in used_b:
            continue
        used_a.add(aid)
        used_b.add(bid)
        a = next(x for x in old if x["id"] == aid)
        b = next(x for x in new if x["id"] == bid)
        delta = SEVERITY_RANK.get(b["severity"], 0) - SEVERITY_RANK.get(a["severity"], 0)
        pairs.append({
            "old": a,
            "new": b,
            "match_distance": round(d, 3),
            "status": "worse" if delta > 0 else "improved" if delta < 0 else "unchanged",
            "severity_delta": delta,
        })

    only_new = [b for b in new if b["id"] not in used_b]
    unmatched_a = [a for a in old if a["id"] not in used_a]

    # An unmatched A defect counts as "resolved" only if B actually covered that
    # spot; otherwise B simply didn't inspect there → "uncovered". Coverage is
    # known only in aligned mode; without it (covered_ids is None) keep the old
    # behaviour and treat every unmatched A defect as potentially resolved.
    if covered_ids is None:
        only_resolved, uncovered = unmatched_a, []
    else:
        only_resolved = [a for a in unmatched_a if a["id"] in covered_ids]
        uncovered = [a for a in unmatched_a if a["id"] not in covered_ids]

    return {
        "pairs": pairs,
        "new": only_new,
        "resolved": only_resolved,
        "uncovered": uncovered,
        "summary": {
            "matched": len(pairs),
            "worse": sum(1 for p in pairs if p["status"] == "worse"),
            "improved": sum(1 for p in pairs if p["status"] == "improved"),
            "unchanged": sum(1 for p in pairs if p["status"] == "unchanged"),
            "new": len(only_new),
            "resolved": len(only_resolved),
            "uncovered": len(uncovered),
        },
    }
