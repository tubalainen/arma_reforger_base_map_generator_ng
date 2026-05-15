"""
OSM-aware descriptive naming for Enfusion SplineShapeEntity / prefab instances.

v1.4.0 (Atlas 2 alignment): replaces the previous sequential-ID scheme
(``Road_0``, ``Road_1``, ``ForestArea_3`` ...) with names derived from the
source data so the World Editor user can immediately tell what each entity
represents. Examples produced by :func:`make_entity_name`:

================= ============================  ===================================
Old name          New name (with OSM context)   When
================= ============================  ===================================
``Road_0``        ``Road_E4_Asphalt_001``        OSM way with ``ref=E4``
``Road_1``        ``Road_Storgatan_Asphalt_001`` OSM way with ``name=Storgatan``
``Road_2``        ``Road_Asphalt_NE_001``        Anonymous way (no ref / name)
``ForestArea_0``  ``Forest_Pine_NE_001``         Anonymous coniferous polygon
``Water_0``       ``Lake_Vanern``                Lake with ``name=Vänern``
``River_0``       ``River_Dalalven``             River with ``name=Dalälven``
``Building_0``    ``Building_Church_StMary``     Building with ``name=St Mary``
``Building_1``    ``Building_Residential_NW_042`` Anonymous residential building
================= ============================  ===================================

Names are sanitised to Enfusion-safe ASCII identifiers and uniqueness is
guaranteed by an :class:`EntityNamer` that tracks seen names across an
entire layer file.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


_DISALLOWED = re.compile(r"[^A-Za-z0-9_]+")
_MULTI_UNDERSCORE = re.compile(r"_{2,}")
_MAX_LEN = 48


# Forest type → display token used in the entity name.
_FOREST_TYPE_DISPLAY: dict[str, str] = {
    "coniferous": "Pine",
    "deciduous": "Deciduous",
    "mixed": "Mixed",
    "scrub": "Scrub",
    "heath": "Heath",
}

# Water type → display token (for closed-spline lakes).
_LAKE_TYPE_DISPLAY: dict[str, str] = {
    "lake": "Lake",
    "pond": "Pond",
    "reservoir": "Reservoir",
    "water": "Water",
}

# Building category → display token.
_BUILDING_DISPLAY: dict[str, str] = {
    "apartments": "Apartments",
    "house": "House",
    "residential": "Residential",
    "commercial": "Commercial",
    "industrial": "Industrial",
    "warehouse": "Warehouse",
    "church": "Church",
    "garage": "Garage",
    "shed": "Shed",
    "barn": "Barn",
    "yes": "Generic",
}


_EXTRA_FOLD = {
    # Atomic letters NFKD won't decompose. Mostly Nordic / Germanic / Baltic.
    "Ø": "O", "ø": "o", "Æ": "AE", "æ": "ae",
    "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th",
    "Ð": "D", "ð": "d", "ß": "ss",
    "Ł": "L", "ł": "l", "Đ": "D", "đ": "d",
}


def sanitize_token(text: str) -> str:
    """
    Reduce an arbitrary text token to a safe Enfusion identifier chunk.

    - ASCII-fold via NFKD ('Vänern' → 'Vanern', 'Älven' → 'Alven').
    - Pre-fold atomic letters NFKD can't decompose (Ø, Æ, Ł, ß, ...).
    - Strip every character that isn't alphanumeric or underscore.
    - Collapse runs of underscores.
    - Title-case multi-word inputs so the result is camel-ish: "St Mary" → "StMary".
    """
    if not text:
        return ""
    # Drop quotes and similar separators before NFKD to avoid spurious tokens.
    text = text.replace("'", "").replace('"', "").replace("`", "")
    # Pre-fold atomic letters NFKD doesn't decompose (Ø, Æ, Ł, ß, ...).
    text = "".join(_EXTRA_FOLD.get(c, c) for c in text)
    # NFKD decomposes accented letters into base + combining mark; encoding to
    # ASCII (errors="ignore") then drops the combining marks. Catches Nordic
    # vowels (ä/ö/å/é) and most other Latin-script accents.
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    # Title-case each whitespace-separated word so we get StMary, NewYork, etc.
    words = [w for w in re.split(r"\s+", folded) if w]
    if not words:
        return ""
    titled = "".join(w[:1].upper() + w[1:] for w in words)
    # Strip any remaining disallowed characters.
    cleaned = _DISALLOWED.sub("_", titled)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("_")
    return cleaned


def _quadrant(
    x_local: float | None,
    z_local: float | None,
    terrain_width: float,
    terrain_depth: float,
) -> str:
    """
    Return one of {NW, NE, SW, SE, C} for an XZ point inside the terrain.
    Falls back to "C" if either coordinate is missing.

    Enfusion uses X=east, Z=north (north-handed). So we map:
      x < width/2 → west,  x >= width/2 → east
      z < depth/2 → south, z >= depth/2 → north
    """
    if x_local is None or z_local is None:
        return "C"
    east = x_local >= terrain_width / 2
    north = z_local >= terrain_depth / 2
    return ("N" if north else "S") + ("E" if east else "W")


def _candidate_from_osm(kind: str, properties: dict) -> Optional[str]:
    """
    Try to derive a descriptive token from OSM tags. Returns the sanitised
    token (without the kind prefix) or None if no usable tag is present.
    """
    props = properties or {}
    ref = props.get("ref") or ""
    name = props.get("name") or ""

    if kind == "Road":
        # ``ref`` (e.g. "E4", "M25") is more compact than ``name`` and usually
        # what players think of the road as. Prefer ref, fall back to name.
        token = sanitize_token(ref) or sanitize_token(name)
        return token or None

    if kind in ("Lake", "River"):
        # Water bodies are best identified by their proper name.
        return sanitize_token(name) or None

    if kind == "Building":
        # Buildings rarely have a name; when they do (churches, schools,
        # landmarks) it's gold-standard context.
        return sanitize_token(name) or None

    # Forests don't usually have names in OSM; fall through to category logic.
    return None


def _category_token(kind: str, properties: dict) -> str:
    """
    Return the type/category token used in the descriptive name, e.g.
    "Asphalt" for a road, "Pine" for a forest, "Church" for a building.
    """
    props = properties or {}

    if kind == "Road":
        surface = (props.get("surface") or props.get("Surface") or "asphalt").lower()
        return surface[:1].upper() + surface[1:].lower() if surface else "Asphalt"

    if kind == "Forest":
        ftype = (props.get("forest_type") or "").lower()
        return _FOREST_TYPE_DISPLAY.get(ftype, "Mixed")

    if kind == "Lake":
        wtype = (props.get("water_type") or "lake").lower()
        return _LAKE_TYPE_DISPLAY.get(wtype, "Lake")

    if kind == "River":
        wtype = (props.get("water_type") or "river").lower()
        return wtype[:1].upper() + wtype[1:].lower()

    if kind == "Building":
        btype = (props.get("building_type") or props.get("building") or "yes").lower()
        return _BUILDING_DISPLAY.get(btype, btype[:1].upper() + btype[1:].lower() or "Generic")

    return kind


class EntityNamer:
    """
    Stateful name allocator for one .layer file's worth of entities.

    Tracks how many names of each (kind, category) class have been used so
    suffixes stay small and meaningful (Road_Asphalt_NE_001 .. _030 rather
    than _0014, _0234, ...). Also dedupes if multiple OSM features happen
    to have the same name (Lake_Vanern, Lake_Vanern_002, ...).
    """

    def __init__(self, terrain_width: float, terrain_depth: float):
        self.terrain_width = float(terrain_width)
        self.terrain_depth = float(terrain_depth)
        # (kind, category, quadrant) → next index for anonymous features
        self._index_counter: dict[tuple[str, str, str], int] = {}
        # All emitted names, for collision-free uniqueness.
        self._seen: set[str] = set()

    def make_name(
        self,
        kind: str,
        properties: Optional[dict] = None,
        x_local: Optional[float] = None,
        z_local: Optional[float] = None,
    ) -> str:
        """
        Build a descriptive, collision-free entity name.

        Priority:
          1. OSM-derived descriptor (``ref`` or ``name``).
          2. ``Kind_Category_<Quadrant>_<NNN>`` for anonymous features.

        Returns a string that is always a valid Enfusion identifier.
        """
        props = properties or {}
        category = _category_token(kind, props)
        descriptor = _candidate_from_osm(kind, props)

        if descriptor:
            # OSM-named features:
            #   Road_E4_Asphalt   /  Road_Storgatan_Asphalt
            #   Lake_Vanern       /  River_Dalalven  /  Building_Church_StMary
            if kind == "Building":
                base = f"{kind}_{category}_{descriptor}"
            elif kind in ("Lake", "River"):
                base = f"{kind}_{descriptor}"
            else:  # Road
                base = f"{kind}_{descriptor}_{category}"
        else:
            quadrant = _quadrant(x_local, z_local, self.terrain_width, self.terrain_depth)
            key = (kind, category, quadrant)
            idx = self._index_counter.get(key, 0) + 1
            self._index_counter[key] = idx
            # River_River_<quad>_NNN is ugly; collapse when category == kind.
            if kind == category or (kind == "River" and category.lower() == "river"):
                base = f"{kind}_{quadrant}_{idx:03d}"
            else:
                base = f"{kind}_{category}_{quadrant}_{idx:03d}"

        base = _MULTI_UNDERSCORE.sub("_", base).strip("_")
        if len(base) > _MAX_LEN:
            base = base[:_MAX_LEN].rstrip("_")

        # Collision suffix (handles two Storgatan asphalt roads, two namesakes, etc.)
        if base not in self._seen:
            self._seen.add(base)
            return base
        n = 2
        while True:
            candidate = f"{base}_{n:03d}"
            if candidate not in self._seen:
                self._seen.add(candidate)
                return candidate
            n += 1


# Surface that the spline is expected to paint underneath itself. Used by
# the per-spline `// paints: <surface>` comment and the
# surface_assignments.json sidecar.
def expected_surface(kind: str, properties: Optional[dict] = None) -> Optional[str]:
    """
    Return the surface mask key the spline is expected to ride on, or None
    if no surface association applies (e.g. buildings).
    """
    props = properties or {}

    if kind == "Road":
        surface = (props.get("surface") or "asphalt").lower()
        if surface in ("asphalt", "gravel", "dirt"):
            return surface
        if surface == "cobblestone":
            return "asphalt"
        return "asphalt"

    if kind == "Forest":
        ftype = (props.get("forest_type") or "").lower()
        if ftype == "coniferous":
            return "pine_floor"
        if ftype in ("deciduous", "mixed", "scrub", "heath"):
            return "forest_floor"
        return "forest_floor"

    if kind in ("Lake", "River"):
        return "water_edge"

    return None
