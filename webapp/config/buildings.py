"""
Building classification + Enfusion prefab catalog.

Same architecture as ``config.roads``: a known-good prefab catalog plus a
validator. When ``KNOWN_BUILDING_PREFABS`` has an entry for a category, the
buildings layer emits a positioned prefab instance; otherwise it falls back
to a closed-spline footprint marker the user wires manually.

v1.4.1 (2026-05-15) populates the catalog with verified paths sourced from
public Arma Reforger mod source on GitHub. The paths below appear in
production ``.layer`` files of community mods (Overthrow, Coalition,
PodvalAR, GB-Map, DarcMods, …) — i.e. they have been opened and saved by
Workbench against a stock Reforger install, proving the resources resolve.

How the catalog was assembled (v1.4.1):
  1. ``gh api search/code -q '"Prefabs/Structures/Houses" extension:layer'``
     across all public repos (~87 hits at sourcing time).
  2. Pulled the contents of 30 layer files.
  3. Regex-extracted every ``Prefabs/Structures/...\\.et`` reference.
  4. Frequency-ranked the unique paths and picked the most-cited base
     variant per category (e.g. ``FarmHouse_E_1L01.et`` had 5 references —
     more than any other farm building).
  5. Cross-checked against ``feature_extractor.extract_building_features``
     category labels so every category produced there has a mapping here.

Issue #73 (Part 2) is the trigger for this change: the user wants buildings
to appear in the editor as real Building_*.et prefab instances, not as
footprint splines. The v1.4.0 generator already emitted descriptive
``Building_<Type>_<Name|Quadrant>_NNN`` names — this commit finishes the
job by giving the auto-placement code real paths to instantiate.

To replace a chosen variant: edit the path in ``KNOWN_BUILDING_PREFABS``
below. To go back to footprint-marker mode for a category: remove its
entry. The catalog is intentionally hand-curated rather than auto-generated
so we never fabricate paths that fail to resolve in Workbench.

Maintenance: when Bohemia adds new building prefabs (or renames existing
ones in a Reforger patch), the layer files in the same mod corpus will be
updated to point at the new names — rerunning the harvest above will surface
the changes and inform the next maintenance pass on this catalog.
"""

from __future__ import annotations

# Path under the ArmaReforger data root where structure prefabs live.
# All paths in KNOWN_BUILDING_PREFABS are relative to the addon root and
# resolve under this base.
BUILDING_PREFAB_BASE = "Prefabs/Structures"

# Verified (category → .et path) mappings sourced from public Reforger mod
# source on GitHub (v1.4.1). Every path in this dict appears in at least
# one shipped community mod's .layer file, which means Workbench has loaded
# it successfully against a stock Reforger install.
#
# Category labels are produced by
# services.feature_extractor.extract_building_features() from OSM
# ``building=<value>`` tags. All ten categories that extractor emits are
# covered below.
KNOWN_BUILDING_PREFABS: dict[str, str] = {
    # Civilian houses ---------------------------------------------------------
    # Single-floor village house. Most-cited single-storey house in the corpus.
    "Building_House": (
        "Prefabs/Structures/Houses/Village/"
        "House_Village_E_1I01/House_Village_E_1I01.et"
    ),
    # 2-floor town house used as the default residential prefab. The "I" in
    # ``2I01`` is the Everon model line (Interior). Three independent mods
    # use this exact path for residential buildings in towns.
    "Building_Residential": (
        "Prefabs/Structures/Houses/Town/"
        "House_Town_E_2I01/House_Town_E_2I01.et"
    ),
    # Apartments: Reforger 1.x doesn't ship a dedicated multi-unit block, so
    # the larger 2-floor "Villa" variant is the closest stand-in. Workbench
    # users can swap to a custom apartments prefab in the editor.
    "Building_Apartments": (
        "Prefabs/Structures/Houses/Villa/"
        "Villa_E_2I01/Villa_E_2I01.et"
    ),

    # Religious ---------------------------------------------------------------
    # v1.15.4 (issue #198): the previous path
    # ``Cultural/Churches/Church_01/Church_01.et`` was *inferred* from the
    # existence of ``Church_01_ruin.et`` and does not exist — the GitHub
    # layer corpus has no reference to it anywhere. The real resources under
    # ``Churches/`` are ``Church_01/Church_01_white.et``,
    # ``Church_01/Church_01_red.et``, ``ChurchSmall_E_01.et`` and
    # ``ChurchSmall_E_01_weathered.et``. ChurchSmall_E_01 is the one with two
    # independent references (HubSesk/QuickTvT_Podval, Sm1g00l/Predador-Core)
    # and reads as a generic village church, so it is the catalogued default.
    "Building_Church": (
        "Prefabs/Structures/Cultural/Churches/ChurchSmall_E_01.et"
    ),

    # Commercial --------------------------------------------------------------
    # Modern shop building — single-storey concrete commercial unit. Used by
    # Overthrow's Test Island for generic shops.
    "Building_Commercial": (
        "Prefabs/Structures/Commercial/Shops/ShopModern_E_01.et"
    ),

    # Industrial / warehouse --------------------------------------------------
    # Office building, the closest "industrial admin" prefab in stock content.
    "Building_Industrial": (
        "Prefabs/Structures/Industrial/Houses/Office_E_01/Office_E_01.et"
    ),

    # Outbuildings ------------------------------------------------------------
    # Garage prefab — comes from the "house addon" family (small civilian
    # outbuilding tied to a village house).
    "Building_Garage": (
        "Prefabs/Structures/Houses/Village/"
        "HouseAddon_Garage_E_01/HouseAddon_Garage_E_01.et"
    ),
    # Barn — most-cited agriculture prefab in the corpus.
    "Building_Barn": (
        "Prefabs/Structures/Agriculture/Barn/Barn_E_03/Barn_E_03_closed.et"
    ),
    # Shed — simple stand-alone wooden shed.
    "Building_Shed": (
        "Prefabs/Structures/Houses/Shed/Shed_01/Shed_01.et"
    ),

    # Default fallback --------------------------------------------------------
    # When OSM has ``building=yes`` (no further detail) we use the single-storey
    # village house as a sensible default. The user can swap to a custom
    # prefab per-building in the editor if needed.
    "Building_Generic": (
        "Prefabs/Structures/Houses/Village/"
        "House_Village_E_1I01/House_Village_E_1I01.et"
    ),
}


# ---------------------------------------------------------------------------
# Per-resource GUID + entity class (issue #198)
# ---------------------------------------------------------------------------
# Until v1.15.3 the buildings layer emitted ``${<addon GUID>}<path>.et`` for
# every building. That form does not resolve in a ``.layer``: Workbench loads
# the world, creates no entity, and logs nothing at all — so every building in
# every generated map was silently dropped (a 7.9 km test lost all 5,991).
# It is the same defect as issue #111, which section 2 of
# docs/ENFUSION_CONTRACT.md already forbids; buildings were simply never
# migrated to the ``WORLD_PREFAB_*`` pattern when #111 was fixed.
#
# Two things were wrong: the GUID must be the ``.et`` file's **own** resource
# GUID (they differ per file, even between variants of one building), and an
# entity class is required left of the ``:``. The reporter on #198 tested five
# spellings with the addon GUID — including one with the correct class — and
# all five failed, which puts the GUID as the primary fault.
#
# Keyed by **path**, not category: the GUID and class are properties of the
# resource, so the two categories that share House_Village_E_1I01
# (Building_House and Building_Generic) need only one entry, and the tables
# cannot disagree with each other about one file.
#
# Provenance (contract §2: "if we can't read a GUID off a real
# Workbench-saved layer, we don't ship it"). Every GUID below was read off
# ``<Class> : "{GUID}<exact path>"`` lines in .layer files in public mod
# repositories — files Workbench itself wrote. Harvested 2026-09-12 via
# ``gh api search/code`` over ``extension:layer``, counting only references
# whose path matches ours **exactly**: the parallel ``PrefabLibrary/...``
# resources carry different GUIDs for the same building (House_Village_E_1I01
# is EDBC0E94793BA9F1 under ``Prefabs/Structures/`` but BB32FDB0A276A95D
# under ``PrefabLibrary/``), and mixing the two would reproduce #111.
#
#   path                      GUID              independent repos
#   House_Village_E_1I01      EDBC0E94793BA9F1  3 + reporter's Workbench 1.8.0.13
#   House_Town_E_2I01         38A5F3E4578087AB  3
#   Villa_E_2I01              5CADC96916FF1CC4  3
#   Office_E_01               51F233A0BA73532A  3
#   HouseAddon_Garage_E_01    448D2BD96AA205E4  3
#   ShopModern_E_01           CFE8511B2B7E8AAA  2
#   Shed_01                   F08D8E78433D713A  2
#   ChurchSmall_E_01          0FD764569422DBA7  2
#   Barn_E_03_closed          C363B659675041BA  1  <-- see caveat below
#
# Caveat: Barn_E_03_closed is single-source (JoshuaHeathcote1987/GB-Map) —
# 11 candidate layer files referenced the name, only that one at our exact
# path. The sibling ``Barn_E_03_open.et`` (66F1A0049CC2F5BD, a different
# repo) confirms the directory is real. Shipping it is still strictly better
# than the addon GUID, which is known-broken, but it is the first entry to
# re-check if a reporter says barns specifically fail to appear.
BUILDING_PREFAB_GUIDS: dict[str, str] = {
    "Prefabs/Structures/Houses/Village/"
    "House_Village_E_1I01/House_Village_E_1I01.et": "EDBC0E94793BA9F1",
    "Prefabs/Structures/Houses/Town/"
    "House_Town_E_2I01/House_Town_E_2I01.et": "38A5F3E4578087AB",
    "Prefabs/Structures/Houses/Villa/"
    "Villa_E_2I01/Villa_E_2I01.et": "5CADC96916FF1CC4",
    "Prefabs/Structures/Cultural/Churches/"
    "ChurchSmall_E_01.et": "0FD764569422DBA7",
    "Prefabs/Structures/Commercial/Shops/"
    "ShopModern_E_01.et": "CFE8511B2B7E8AAA",
    "Prefabs/Structures/Industrial/Houses/"
    "Office_E_01/Office_E_01.et": "51F233A0BA73532A",
    "Prefabs/Structures/Houses/Village/"
    "HouseAddon_Garage_E_01/HouseAddon_Garage_E_01.et": "448D2BD96AA205E4",
    "Prefabs/Structures/Agriculture/Barn/"
    "Barn_E_03/Barn_E_03_closed.et": "C363B659675041BA",
    "Prefabs/Structures/Houses/Shed/"
    "Shed_01/Shed_01.et": "F08D8E78433D713A",
}

# Entity class to emit left of the inheritance `:`. Unlike the world prefabs
# — where the class varies per prefab and is not derivable from the path —
# every stock *building* in the harvested corpus instantiates as
# ``SCR_DestructibleBuildingEntity``, across all nine of our paths and all
# 14 source repositories. The table is still explicit per path rather than a
# single constant, because the contract's rule is that the class is read off
# a reference, not assumed; a future non-destructible prefab (a static ruin,
# say, which the corpus shows as ``StaticModelEntity``) would need its own
# entry here rather than a special case in the emitter.
BUILDING_PREFAB_CLASS: dict[str, str] = {
    path: "SCR_DestructibleBuildingEntity" for path in BUILDING_PREFAB_GUIDS
}

# Fail at import time if the three tables ever disagree. KNOWN_BUILDING_PREFABS
# is the catalogue the extractor reads; a path in it with no GUID would emit an
# unresolvable entity, which is the #198 bug class all over again and is
# silent in Workbench. Cheap to check, impossible to forget.
_missing_guids = sorted(
    set(KNOWN_BUILDING_PREFABS.values()) - set(BUILDING_PREFAB_GUIDS)
)
if _missing_guids:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "config.buildings: these KNOWN_BUILDING_PREFABS paths have no entry "
        f"in BUILDING_PREFAB_GUIDS: {_missing_guids}. Capture the real "
        "resource GUID (right-click -> Copy Resource GUID in the Resource "
        "Browser, or read it off a Workbench-saved .layer) before shipping "
        "the path -- see docs/ENFUSION_CONTRACT.md section 2."
    )
del _missing_guids


def building_prefab_reference(path: str | None) -> tuple[str, str] | None:
    """Return ``(entity_class, guid)`` for a catalogued building prefab path.

    ``None`` when the path has no verified GUID, which the buildings-layer
    emitter treats as "skip this building and log it" — never as "emit it
    with the addon GUID", the #198 defect.
    """
    if not path:
        return None
    guid = BUILDING_PREFAB_GUIDS.get(path)
    if not guid:
        return None
    return BUILDING_PREFAB_CLASS.get(path, "SCR_DestructibleBuildingEntity"), guid


def validate_building_prefab(category: str | None) -> str | None:
    """
    Look up the verified Enfusion prefab path for a building category.

    Returns the full ``Prefabs/Structures/.../<Whatever>.et`` string if the
    category has a mapping in ``KNOWN_BUILDING_PREFABS``, otherwise returns
    ``None``.

    The buildings-layer emitter uses ``None`` as a signal to fall back to
    footprint-outline mode (a visible closed spline the user can wire
    manually) rather than fabricating a prefab path.
    """
    if not category:
        return None
    return KNOWN_BUILDING_PREFABS.get(category)
