"""
Spline cleanup helpers — dedup, union, hairpin removal, adaptive simplify.

This module runs **after OSM ingest** and **before** the geometry is projected
to local XZ metres in :mod:`enfusion_project_generator`.  It exists to fix
three classes of artefact seen in the Enfusion World Editor (issues #93, #88):

1. **Full duplicates** — OSM tags the same feature both as a ``way`` and a
   ``relation`` (e.g. ``natural=water`` + ``natural=water`` relation).  Our
   Overpass queries fetch both, so the feature appears twice in the layer.
2. **Partial duplicates** — adjacent same-type polygons (two touching forests,
   a multipolygon's separate outer rings) each become independent splines that
   overlap visually.
3. **Spirals / loops** — open polylines (rivers) with tight hairpin bends, or
   simplified rings, can collapse into self-intersecting curves.

The fix is to (a) ``unary_union`` same-type polygons so overlapping geometry
collapses into one, (b) drop near-reversal vertices on polylines before
simplification, and (c) use an adaptive simplify tolerance that scales with
feature size so small features keep detail and large ones get sparse.

Coastlines are intentionally NOT processed — they must not be unioned with
inland water and they should keep their original shape.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def normalize_polygons(
    features: list[dict],
    kind: str,
    *,
    min_area_m2: float = 100.0,
) -> list[dict]:
    """
    Dedup + union same-type polygon features.

    *features* is a list of GeoJSON-style Feature dicts (``{"type": "Feature",
    "geometry": {...}, "properties": {...}}``) in WGS84 lon/lat.  Polygons and
    MultiPolygons are unioned into a single shapely geometry; the result is
    split back into individual Polygons (one Feature per Polygon).

    Non-polygon features (e.g. LineStrings, coastlines) pass through unchanged.

    *kind* is a human-readable label used in log lines only
    (``"forest"`` / ``"lake"`` / ``"wetland"``).

    Properties are carried forward from the **richest** intersecting input
    feature — preferring named features, then largest area.  Tiny slivers
    smaller than *min_area_m2* (computed in degrees² → approximate m² via the
    feature's centroid latitude) are dropped to prevent noise splines after
    union.
    """
    if not features:
        return features

    try:
        from shapely.geometry import Polygon, MultiPolygon, shape, mapping
        from shapely.ops import unary_union
    except ImportError:
        logger.warning(
            "shapely unavailable — skipping polygon normalisation for %s", kind
        )
        return features

    polygonal: list[tuple[dict, "Polygon"]] = []
    passthrough: list[dict] = []

    for feat in features:
        geom_type = (feat.get("geometry") or {}).get("type", "")
        if geom_type not in ("Polygon", "MultiPolygon"):
            passthrough.append(feat)
            continue
        try:
            geom = shape(feat["geometry"])
        except Exception as exc:  # malformed geometry
            logger.debug("dropping malformed %s feature: %s", kind, exc)
            continue
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            continue
        if isinstance(geom, MultiPolygon):
            for sub in geom.geoms:
                if not sub.is_empty:
                    polygonal.append((feat, sub))
        else:
            polygonal.append((feat, geom))

    if not polygonal:
        return passthrough + features  # nothing polygonal to merge

    union_geom = unary_union([g for _, g in polygonal])
    if union_geom.is_empty:
        return passthrough

    if isinstance(union_geom, Polygon):
        union_parts = [union_geom]
    elif hasattr(union_geom, "geoms"):
        union_parts = [g for g in union_geom.geoms if not g.is_empty]
    else:
        union_parts = []

    # Approx degrees² → m² at the dataset centroid latitude (good enough for
    # filtering slivers; we are not doing accurate area accounting).
    try:
        centroid_lat = union_geom.centroid.y
    except Exception:
        centroid_lat = 0.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(centroid_lat)), 1e-6)
    deg2_to_m2 = m_per_deg_lat * m_per_deg_lon
    min_area_deg2 = min_area_m2 / deg2_to_m2 if deg2_to_m2 > 0 else 0.0

    out: list[dict] = list(passthrough)
    dropped_slivers = 0
    for part in union_parts:
        if part.area < min_area_deg2:
            dropped_slivers += 1
            continue
        props = _best_props([(f, g) for f, g in polygonal if g.intersects(part)])
        out.append(
            {
                "type": "Feature",
                "geometry": mapping(part),
                "properties": props,
            }
        )

    n_in = len(features)
    n_out_polys = len(out) - len(passthrough)
    n_in_polys = len(polygonal)
    merged = n_in_polys - n_out_polys
    if merged > 0 or dropped_slivers > 0:
        logger.info(
            "spline_cleanup[%s]: %d in → %d out (merged %d, dropped %d slivers)",
            kind,
            n_in,
            len(out),
            merged,
            dropped_slivers,
        )
    return out


def normalize_polylines(
    features: list[dict],
    kind: str,
) -> list[dict]:
    """
    Drop hairpin vertices on open polyline features (rivers, streams, canals).

    A *hairpin* is an interior vertex where the bearing reverses by more than
    150° AND the neighbouring vertices are within 20 m of each other — the
    classic signature of a single bad OSM node or an over-simplified bend that
    will render as a spiral loop in the World Editor.

    Operates in WGS84 lon/lat by converting to local metres around each
    candidate's latitude.  Iterates up to 5 times to catch chained hairpins.

    Returns a NEW list of Feature dicts.  Features whose geometry collapses to
    fewer than 2 points are dropped.
    """
    if not features:
        return features

    out: list[dict] = []
    total_dropped = 0
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type", "")
        if gtype != "LineString":
            out.append(feat)
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 3:
            out.append(feat)
            continue
        cleaned, dropped = _drop_hairpins_lonlat(coords)
        total_dropped += dropped
        if len(cleaned) < 2:
            continue  # collapsed to nothing — skip
        new_feat = dict(feat)
        new_feat["geometry"] = {"type": "LineString", "coordinates": cleaned}
        out.append(new_feat)

    if total_dropped > 0:
        logger.info(
            "spline_cleanup[%s]: dropped %d hairpin vertices across %d polylines",
            kind,
            total_dropped,
            len(features),
        )
    return out


def adaptive_tolerance(pts: list[dict], *, lo: float = 1.0, hi: float = 5.0) -> float:
    """
    Pick a simplify tolerance in metres scaled to feature size.

    *pts* is a list of ``{"x", "y", "z"}`` dicts in local metres (post
    projection).  Returns 0.5 % of the bbox diagonal, clamped to ``[lo, hi]``.
    Small features (~50 m) get ~1 m tolerance (keeps small bays / peninsulas);
    large features (~2 km) get ~5 m (sparse like a hand-drawn outline).
    """
    if not pts:
        return lo
    xs = [p["x"] for p in pts]
    zs = [p["z"] for p in pts]
    diag = math.hypot(max(xs) - min(xs), max(zs) - min(zs))
    return max(lo, min(hi, diag * 0.005))


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _best_props(candidates: list[tuple[dict, "object"]]) -> dict:
    """
    Pick the richest property dict from the input features whose geometry
    contributed to a unioned part.  Preference order:
        1. Has a non-empty ``name``
        2. Largest source geometry area
    Falls back to the first feature's properties if no other tiebreak.
    """
    if not candidates:
        return {}

    def _key(item):
        feat, geom = item
        props = feat.get("properties") or {}
        named = bool((props.get("name") or "").strip())
        area = getattr(geom, "area", 0.0) or 0.0
        return (1 if named else 0, area)

    best_feat, _ = max(candidates, key=_key)
    return dict(best_feat.get("properties") or {})


def _drop_hairpins_lonlat(
    coords: list[list[float]],
    *,
    min_turn_deg: float = 150.0,
    min_span_m: float = 20.0,
    max_passes: int = 5,
) -> tuple[list[list[float]], int]:
    """
    Iterative hairpin-vertex removal on a WGS84 ``[lon, lat]`` polyline.

    Returns ``(cleaned_coords, num_dropped)``.
    """
    pts = list(coords)
    total_dropped = 0
    for _ in range(max_passes):
        if len(pts) < 3:
            break
        keep = [True] * len(pts)
        for i in range(1, len(pts) - 1):
            if not keep[i]:
                continue
            a = pts[i - 1]
            b = pts[i]
            c = pts[i + 1]
            turn = _turn_angle_deg(a, b, c)
            if turn < min_turn_deg:
                continue
            if _haversine_m(a, c) > min_span_m:
                continue
            keep[i] = False
        if all(keep):
            break
        dropped = keep.count(False)
        total_dropped += dropped
        pts = [p for p, k in zip(pts, keep) if k]
    return pts, total_dropped


def _turn_angle_deg(
    a: list[float], b: list[float], c: list[float]
) -> float:
    """
    Absolute interior turn angle in degrees at vertex ``b`` of the path
    ``a → b → c``.  0° = straight, 180° = full reversal (hairpin).

    Computed in a local metres frame around ``b`` to avoid lon-distortion at
    high latitudes.
    """
    lat = b[1]
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat)), 1e-6)

    def to_xy(p):
        return (
            (p[0] - b[0]) * m_per_deg_lon,
            (p[1] - b[1]) * m_per_deg_lat,
        )

    ax, ay = to_xy(a)
    cx, cy = to_xy(c)
    # Incoming bearing a→b (so vector from a to b is -a in local frame)
    inc_dx, inc_dy = -ax, -ay
    out_dx, out_dy = cx, cy
    inc_len = math.hypot(inc_dx, inc_dy)
    out_len = math.hypot(out_dx, out_dy)
    if inc_len < 1e-9 or out_len < 1e-9:
        return 0.0
    cos_t = (inc_dx * out_dx + inc_dy * out_dy) / (inc_len * out_len)
    cos_t = max(-1.0, min(1.0, cos_t))
    # Turn angle = angle between the incoming (a→b) and outgoing (b→c) vectors.
    # cos_t = 1  → vectors aligned → 0° turn (straight)
    # cos_t = -1 → vectors opposite → 180° turn (hairpin / full reversal)
    return math.degrees(math.acos(cos_t))


def _haversine_m(a: list[float], b: list[float]) -> float:
    """Great-circle distance between two ``[lon, lat]`` points in metres."""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))
