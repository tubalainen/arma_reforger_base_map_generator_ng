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

**Clip before you union (issue #170).**  Overpass returns whole ways and
relations that merely *touch* the requested bbox, so a 2.8 km map can arrive
carrying 5 500 km² of lake — hundreds of thousands of vertices of which
99.9 % lie outside the terrain and are discarded later by
``_clip_ring_to_terrain``.  Unioning that first made the cleanup take tens of
minutes and blocked the whole pipeline.  :func:`normalize_polygons` therefore
takes ``clip_bounds`` and intersects every input polygon with the (padded) map
rectangle *before* any union, dedup or property matching.  A bbox intersection
is near-linear in vertex count, whereas ``unary_union`` is emphatically not:
on a synthetic reproduction of the reported job (39 lake polygons, 208 k
vertices, on a 2.82 km map) the water layer went from minutes to ~1 s.

Order matters twice over.  The clip runs *before* the ``buffer(0)`` validity
repair, because repairing an invalid 200 k-vertex lake is itself one of the
expensive operations being avoided, and it runs before ``unary_union`` because
that is the quadratic step.  The emitted splines are unchanged — the terrain
clip downstream would have cut the same geometry away regardless.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Padding applied to the clip rectangle, in metres.
#
# This collar has to be wider than every way the terrain rectangle can reach
# past the drawn WGS84 bbox, or the pre-clip would become a behaviour change
# instead of a speed-up:
#   * snap_to_tile_multiple() rounds the grid to the NEAREST 128-face tile, so
#     the terrain can exceed the drawn square by up to 64 faces × 2 m = 128 m
#     per side (the frontend auto-resizes the square to match, so in practice
#     this is ~0, but the pipeline must not depend on that);
#   * lake rings are dilated outward by LAKE_RING_BUFFER_M (5 m) before the
#     local-metre terrain clip;
#   * _clip_ring_to_terrain adds a further 1 m margin.
# 500 m leaves roughly 3.7× headroom over that 134 m worst case while still
# discarding the out-of-map bulk the issue is about.
#
# This is the collar for *direct* callers. The pipeline's caller,
# EnfusionProjectGenerator._expand_to_terrain, additionally measures the real
# overhang through the live transformer and widens the bounds before handing
# them down, so it does not depend on the constants above being right.
CLIP_PAD_M = 500.0

# Above this vertex count a single clipped ring is pre-simplified before the
# union. OSM shorelines can carry a vertex every few centimetres; splines are
# capped at MAX_SPLINE_POINTS_NATURAL (120) points and simplified at a 1–5 m
# tolerance anyway, so shaving sub-metre detail off a monster ring costs nothing
# visible and bounds the worst case. Rings below the threshold are untouched.
PRESIMPLIFY_VERTEX_THRESHOLD = 2000
PRESIMPLIFY_TOLERANCE_M = 0.5


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def normalize_polygons(
    features: list[dict],
    kind: str,
    *,
    min_area_m2: float = 100.0,
    clip_bounds: Optional[tuple[float, float, float, float]] = None,
) -> list[dict]:
    """
    Clip to the map rectangle, then dedup + union same-type polygon features.

    *features* is a list of GeoJSON-style Feature dicts (``{"type": "Feature",
    "geometry": {...}, "properties": {...}}``) in WGS84 lon/lat.  Polygons and
    MultiPolygons are unioned into a single shapely geometry; the result is
    split back into individual Polygons (one Feature per Polygon).

    Non-polygon features (e.g. LineStrings, coastlines) pass through unchanged.

    *kind* is a human-readable label used in log lines only
    (``"forest"`` / ``"lake"`` / ``"wetland"``).

    *clip_bounds* is the map's WGS84 ``(west, south, east, north)``.  When
    supplied — it always is in the pipeline — every input polygon is first
    intersected with that rectangle padded by :data:`CLIP_PAD_M`, so the union
    only ever sees geometry that can reach the terrain.  This is the fix for
    issue #170; see the module docstring.  Omitting it preserves the old
    whole-planet behaviour and is only useful in tests.

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

    started = time.perf_counter()
    clip_box = _clip_box(clip_bounds)

    polygonal: list[tuple[dict, "Polygon"]] = []
    passthrough: list[dict] = []
    clipped_away = 0
    vertices_in = 0
    vertices_clipped = 0
    presimplified = 0

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
        vertices_in += _count_vertices(geom)
        if geom.is_empty:
            continue

        # --- issue #170: discard the out-of-map bulk before anything costly ---
        # Clip BEFORE the validity repair, not after: buffer(0) on an invalid
        # 200 k-vertex OSM lake is itself one of the expensive operations this
        # is meant to avoid, and repairing the clipped remnant gives the same
        # result for a fraction of the work.
        if clip_box is not None:
            geom = _clip_to_box(geom, clip_box)
            if geom is None or geom.is_empty:
                clipped_away += 1
                continue

        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            continue

        parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
        for sub in parts:
            if sub.is_empty or not isinstance(sub, Polygon):
                continue
            sub, did_simplify = _presimplify(sub)
            if sub is None or sub.is_empty:
                continue
            presimplified += 1 if did_simplify else 0
            vertices_clipped += _count_vertices(sub)
            polygonal.append((feat, sub))

    if not polygonal:
        # Everything polygonal fell outside the map (or there was none to start
        # with). Return only what genuinely passes through — the old code
        # returned `passthrough + features`, duplicating every passthrough
        # feature, which then produced doubled river splines.
        _log_polygon_cleanup(
            kind, len(features), len(passthrough), 0, 0, 0, clipped_away,
            len(passthrough), min_area_m2, vertices_in, 0, presimplified,
            time.perf_counter() - started,
        )
        return list(passthrough)

    union_geom = unary_union([g for _, g in polygonal])
    if union_geom.is_empty:
        return list(passthrough)

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

    # Property matching used to test every source polygon against every union
    # part — O(parts × inputs) full-geometry intersects with no index. An
    # STRtree turns that into a bbox lookup plus a handful of exact tests.
    matcher = _PropertyMatcher(polygonal)

    out: list[dict] = list(passthrough)
    dropped_slivers = 0
    for part in union_parts:
        if part.area < min_area_deg2:
            dropped_slivers += 1
            continue
        out.append(
            {
                "type": "Feature",
                "geometry": mapping(part),
                "properties": matcher.props_for(part),
            }
        )

    n_out_polys = len(out) - len(passthrough)
    _log_polygon_cleanup(
        kind,
        len(features),
        len(out),
        len(polygonal),
        n_out_polys,
        dropped_slivers,
        clipped_away,
        len(passthrough),
        min_area_m2,
        vertices_in,
        vertices_clipped,
        presimplified,
        time.perf_counter() - started,
    )
    return out


def normalize_polylines(
    features: list[dict],
    kind: str,
    *,
    clip_bounds: Optional[tuple[float, float, float, float]] = None,
) -> list[dict]:
    """
    Drop hairpin vertices on open polyline features (rivers, streams, canals).

    A *hairpin* is an interior vertex where the bearing reverses by more than
    150° AND the neighbouring vertices are within 20 m of each other — the
    classic signature of a single bad OSM node or an over-simplified bend that
    will render as a spiral loop in the World Editor.

    Operates in WGS84 lon/lat by converting to local metres around each
    candidate's latitude.  Iterates up to 5 times to catch chained hairpins.

    *clip_bounds* is the map's WGS84 ``(west, south, east, north)``.  Overpass
    hands back whole waterways, so a stream that only clips the corner of the
    map arrives as the entire 200 km river.  Features whose bounding box misses
    the padded map rectangle entirely are dropped up front (#170) — the
    downstream in-bounds point filter would have discarded them anyway, so the
    emitted splines are unchanged.  Geometry that *does* reach the map is left
    intact, hairpin removal included.

    Returns a NEW list of Feature dicts.  Features whose geometry collapses to
    fewer than 2 points are dropped.
    """
    if not features:
        return features

    started = time.perf_counter()
    reject = _bbox_rejector(clip_bounds)

    out: list[dict] = []
    total_dropped = 0
    off_map = 0
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type", "")
        if gtype != "LineString":
            out.append(feat)
            continue
        coords = geom.get("coordinates") or []
        if reject is not None and reject(coords):
            off_map += 1
            continue
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

    collapsed = len(features) - len(out) - off_map
    logger.info(
        "Spline cleanup [%s]: %d polyline(s) checked for hairpins in %.2fs — "
        "%d vertex/vertices dropped, %d feature(s) collapsed and removed, "
        "%d dropped as fully outside the map",
        kind,
        len(features),
        time.perf_counter() - started,
        total_dropped,
        max(0, collapsed),
        off_map,
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


class _PropertyMatcher:
    """
    Indexed replacement for the ``[... if g.intersects(part)]`` scan (#170).

    The old code ran a full-geometry ``intersects`` for every (union part,
    source polygon) pair. With an STRtree the bbox filter is done in C and only
    genuine bbox overlaps reach the exact predicate, which is what makes this
    linear-ish instead of quadratic.

    Falls back to the exhaustive scan if STRtree is unavailable so behaviour is
    identical on an older shapely.
    """

    def __init__(self, polygonal: list[tuple[dict, "object"]]):
        self._polygonal = polygonal
        self._tree = None
        try:
            from shapely import STRtree

            self._tree = STRtree([g for _, g in polygonal])
        except Exception:  # pragma: no cover - shapely <2.0 / import failure
            logger.debug("STRtree unavailable — property matching stays linear")

    def props_for(self, part) -> dict:
        if self._tree is None:
            return _best_props(
                [(f, g) for f, g in self._polygonal if g.intersects(part)]
            )
        try:
            idx = self._tree.query(part, predicate="intersects")
        except Exception:  # pragma: no cover - defensive
            return _best_props(
                [(f, g) for f, g in self._polygonal if g.intersects(part)]
            )
        candidates = [self._polygonal[i] for i in idx]
        if not candidates:
            # A union part always comes from at least one input, but a
            # predicate miss (precision) must not lose the properties.
            candidates = [
                (f, g) for f, g in self._polygonal if g.intersects(part)
            ]
        return _best_props(candidates)


def _bbox_rejector(bounds: Optional[tuple[float, float, float, float]]):
    """
    Return ``reject(coords) -> bool``, true when a lon/lat coordinate list lies
    wholly outside the padded map rectangle — or ``None`` when *bounds* is
    unusable, in which case nothing is rejected.

    A plain min/max sweep, no shapely: this exists to avoid work, so it must
    stay cheaper than the work it skips.
    """
    padded = _padded_bounds(bounds)
    if padded is None:
        return None
    west, south, east, north = padded

    def reject(coords) -> bool:
        if not coords:
            return True
        lons = [c[0] for c in coords if len(c) >= 2]
        lats = [c[1] for c in coords if len(c) >= 2]
        if not lons:
            return True
        return (
            max(lons) < west
            or min(lons) > east
            or max(lats) < south
            or min(lats) > north
        )

    return reject


def _padded_bounds(
    bounds: Optional[tuple[float, float, float, float]]
) -> Optional[tuple[float, float, float, float]]:
    """``(west, south, east, north)`` grown by :data:`CLIP_PAD_M`, or ``None``."""
    if not bounds or len(bounds) != 4:
        return None
    try:
        west, south, east, north = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if not (east > west and north > south):
        logger.warning("Ignoring degenerate clip bounds %r", bounds)
        return None
    mid_lat = (south + north) / 2.0
    pad_lat = CLIP_PAD_M / 111_320.0
    pad_lon = CLIP_PAD_M / (111_320.0 * max(math.cos(math.radians(mid_lat)), 1e-6))
    return (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)


def _clip_box(bounds: Optional[tuple[float, float, float, float]]):
    """
    Build the padded WGS84 clip rectangle, or ``None`` when *bounds* is absent
    or unusable.  Padding is :data:`CLIP_PAD_M` metres converted to degrees at
    the rectangle's own latitude.
    """
    padded = _padded_bounds(bounds)
    if padded is None:
        return None
    try:
        from shapely.geometry import box
    except ImportError:  # pragma: no cover - shapely is a hard dep
        return None
    return box(*padded)


def _clip_to_box(geom, clip_box):
    """
    Intersect *geom* with *clip_box*, returning ``None`` when nothing survives.

    Wholly-inside geometry is returned untouched so the common small-feature
    case costs one cheap ``contains`` test and no new allocation.
    """
    from shapely.geometry import MultiPolygon, Polygon

    # Bounding-box rejection first, read straight off the envelope: no GEOS
    # predicate, and it is safe on invalid geometry (which we deliberately have
    # not repaired yet). Most features on a small map die right here.
    try:
        minx, miny, maxx, maxy = geom.bounds
        bminx, bminy, bmaxx, bmaxy = clip_box.bounds
        if maxx < bminx or minx > bmaxx or maxy < bminy or miny > bmaxy:
            return None
        if minx >= bminx and maxx <= bmaxx and miny >= bminy and maxy <= bmaxy:
            return geom  # wholly inside — nothing to cut
    except Exception:  # noqa: BLE001 - no usable envelope; fall through
        pass

    try:
        clipped = geom.intersection(clip_box)
    except Exception:  # noqa: BLE001 - invalid input; repair, then retry once
        try:
            clipped = geom.buffer(0).intersection(clip_box)
        except Exception:  # noqa: BLE001 - a GEOS blow-up must not abort a job
            logger.debug("clip failed for one polygon; keeping it unclipped")
            return geom
    if clipped.is_empty:
        return None
    if isinstance(clipped, (Polygon, MultiPolygon)):
        return clipped
    # GeometryCollection: keep only the polygonal members (an edge-tangent
    # polygon can clip down to a line or a point).
    polys = [g for g in getattr(clipped, "geoms", []) if isinstance(g, Polygon)]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _presimplify(poly):
    """
    Shave sub-metre noise off a pathologically dense ring before the union.

    Returns ``(polygon_or_None, did_simplify)``.  Rings with fewer than
    :data:`PRESIMPLIFY_VERTEX_THRESHOLD` vertices are returned untouched, which
    is nearly all of them once the bbox clip has run.
    """
    n = _count_vertices(poly)
    if n < PRESIMPLIFY_VERTEX_THRESHOLD:
        return poly, False
    try:
        # Degrees are anisotropic away from the equator; converting via the
        # latitude axis keeps the tolerance at or below the metre budget on
        # both axes, so this can never cut more than PRESIMPLIFY_TOLERANCE_M.
        tol_deg = PRESIMPLIFY_TOLERANCE_M / 111_320.0
        simplified = poly.simplify(tol_deg, preserve_topology=True)
        if simplified.is_empty or simplified.geom_type != "Polygon":
            return poly, False
        return simplified, True
    except Exception:  # noqa: BLE001 - defensive; detail is not worth a crash
        return poly, False


def _count_vertices(geom) -> int:
    """Total exterior+interior vertex count of a (Multi)Polygon, 0 on failure."""
    try:
        if geom.geom_type == "Polygon":
            return len(geom.exterior.coords) + sum(
                len(r.coords) for r in geom.interiors
            )
        if hasattr(geom, "geoms"):
            return sum(_count_vertices(g) for g in geom.geoms)
    except Exception:  # noqa: BLE001 - logging aid only
        pass
    return 0


def _log_polygon_cleanup(
    kind: str,
    n_in: int,
    n_out: int,
    n_in_polys: int,
    n_out_polys: int,
    dropped_slivers: int,
    clipped_away: int,
    n_passthrough: int,
    min_area_m2: float,
    vertices_in: int,
    vertices_clipped: int,
    presimplified: int,
    elapsed_s: float,
) -> None:
    """
    Report one polygon-cleanup run.

    Always emitted, even for a no-op: silence used to make it look like the
    stage never ran. The vertex counts and elapsed time were added for #170 —
    a nine-minute silent stage is exactly what made that issue hard to read.
    """
    logger.info(
        "Spline cleanup [%s]: %d feature(s) in → %d out in %.2fs — %d polygon(s) "
        "unioned into %d, %d merged away, %d dropped as fully outside the map, "
        "%d sliver(s) dropped (<%.0f m²), %d non-polygon passthrough; "
        "vertices %d → %d after clip (%d ring(s) pre-simplified)",
        kind,
        n_in,
        n_out,
        elapsed_s,
        n_in_polys,
        n_out_polys,
        max(0, n_in_polys - n_out_polys),
        clipped_away,
        dropped_slivers,
        min_area_m2,
        n_passthrough,
        vertices_in,
        vertices_clipped,
        presimplified,
    )


class ElevationIndex:
    """
    Fast ``(x, z) → y`` lookup over a projected ring (issue #170).

    Shapely hands back derived coordinates in four places — polygon clipping,
    ring buffering, RDP simplification and ``buffer(0)`` repair — and each one
    needs an elevation for the resulting vertex. The old code answered every
    query with ``min(points, key=…)``: a full Python scan of the *unclipped*
    ring. For one big OSM lake (200 k source vertices, a few hundred output
    coordinates) that is ~10⁸ distance evaluations per ring, per stage.

    Douglas-Peucker and polygon clipping both **reuse** original vertices for
    everything except the handful of points where a ring crosses the clip edge,
    so an exact-coordinate dict answers nearly every query in O(1).  The rest
    fall through to a KD-tree, or to the linear scan when SciPy is missing.

    Coordinates are keyed at millimetre precision, matching the 3-decimal
    rounding that ``CoordinateTransformer.transform_points`` already applies.
    """

    __slots__ = ("_exact", "_points", "_tree", "_coords")

    def __init__(self, points: list[dict]):
        self._points = points
        self._exact: dict[tuple[float, float], float] = {}
        for p in points:
            self._exact.setdefault((round(p["x"], 3), round(p["z"], 3)), p["y"])
        self._tree = None
        self._coords = None

    def y(self, x: float, z: float) -> float:
        """Elevation of the source vertex at, or nearest to, ``(x, z)``."""
        if not self._points:
            return 0.0
        hit = self._exact.get((round(x, 3), round(z, 3)))
        if hit is not None:
            return hit
        tree = self._ensure_tree()
        if tree is not None:
            try:
                _, idx = tree.query((x, z))
                return self._points[int(idx)]["y"]
            except Exception:  # noqa: BLE001 - fall back to the linear scan
                pass
        nearest = min(
            self._points, key=lambda p: (p["x"] - x) ** 2 + (p["z"] - z) ** 2
        )
        return nearest["y"]

    def _ensure_tree(self):
        if self._tree is not None:
            return self._tree
        if self._coords is False:  # a previous build failed; don't retry
            return None
        try:
            import numpy as np
            from scipy.spatial import cKDTree

            self._coords = np.asarray(
                [(p["x"], p["z"]) for p in self._points], dtype=float
            )
            self._tree = cKDTree(self._coords)
        except Exception:  # noqa: BLE001 - SciPy optional at this layer
            self._coords = False
            return None
        return self._tree


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
