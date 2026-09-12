"""
Raster dimension/encoding contract for generated terrain inputs.

The Enfusion World Editor bakes the terrain's raster inputs (heightmap, surface
masks, satellite map) into per-block textures. A mismatched dimension or an
unexpected channel/profile is a documented cause of access-violation crashes on
the first paint stroke (issues #100/#111/#115/#138). For a terrain of ``N`` faces
per axis the contract is:

* ``heightmap.asc``   -> ``(N+1) x (N+1)``  (faces + 1 vertices)
* ``surface_*.png``   -> ``N x N``          (face resolution, 8-bit grayscale "L")
* ``satellite_map.png`` -> any size, but ``N_x : N_z`` aspect ratio, plain
  8-bit ``RGB`` (no alpha / ICC profile)

``validate_and_harden_rasters`` checks every emitted raster against this
contract, *auto-fixes* encoding issues it can fix safely (mask mode, satellite
alpha/profile), and returns a structured report. Dimension mismatches are
reported as issues (and logged loudly) rather than silently resampled — a wrong
size means an upstream defect that must be found, not papered over.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

# How far the satellite's aspect ratio may drift from the terrain's before it
# counts as a defect. An exact match is not always reachable, for two reasons:
#
#   * The satellite is sized from heightmap *vertices* (N+1, so it never loses
#     detail against the heightmap) but covers the terrain's *face* extent
#     (N x cell size). 129/257 is not 128/256 — 0.39% out on the smallest
#     terrain. The gap closes as the terrain grows (8193/4097 vs 8192/4096 is
#     0.01%), so it only ever shows up where it cannot matter.
#   * At SATELLITE_MAX_DIM both axes are rounded to whole pixels.
#
# Measured worst case over every valid 128-face-multiple grid up to 16384:
# 0.39% within 2:1, 0.78% within 16:1, 1.51% at the 128:1 extreme (a 66 px
# axis). 2% covers all of it and still catches the bug this check exists for —
# pre-#197, a 4096x2048-face terrain got a square texture, a 100% error.
SATELLITE_ASPECT_TOLERANCE = 0.02

# Maximum fraction of satellite_map.png that may be pure nodata (all three
# bands exactly 0) before it counts as a defect.
#
# Every tiled imagery source composites into a zero-filled buffer, so a fetch
# that loses a tile leaves a solid black block rather than failing. SE_59N_14E
# shipped with 7% of its orthophoto black in one corner and passed every check
# we had, because the contract only ever looked at dimensions and mode.
#
# Measured on that same real orthophoto, the natural rate of all-three-bands-
# zero pixels is *exactly zero* outside the hole — even deep lake water bottoms
# out at (0, 16, 12) — so this is an unambiguous signal, and the threshold only
# needs to leave room for the odd compression artifact.
#
# This backstops the per-source coverage gates (e.g. STAC Bild's
# MAX_NODATA_FRACTION): it catches a void from any source, including one
# introduced after the fetch by reprojection or resampling.
SATELLITE_MAX_NODATA_FRACTION = 0.005


def parse_asc_header_dims(path: Path) -> tuple[int, int]:
    """Return ``(ncols, nrows)`` from an ESRI ASCII grid header."""
    ncols = nrows = None
    with open(path, "r") as f:
        for _ in range(8):  # header is the first ~6 lines
            line = f.readline()
            if not line:
                break
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0].lower()
            if key == "ncols":
                ncols = int(float(parts[1]))
            elif key == "nrows":
                nrows = int(float(parts[1]))
            if ncols is not None and nrows is not None:
                break
    if ncols is None or nrows is None:
        raise ValueError(f"Could not parse ncols/nrows from {path}")
    return ncols, nrows


def validate_and_harden_rasters(
    output_dir: Path,
    faces_x: int,
    faces_z: int,
    job: Optional[object] = None,
) -> dict:
    """
    Validate the generated rasters against the terrain face grid and harden the
    PNG encodings in place.

    Args:
        output_dir: The job's Sourcefiles output directory.
        faces_x: Terrain faces per axis (X).
        faces_z: Terrain faces per axis (Z).
        job: Optional job with ``add_log(msg, level)`` for user-visible warnings.

    Returns:
        A report dict: ``{"ok": bool, "terrain_faces": [fx, fz],
        "heightmap": {...}, "masks": [...], "satellite": {...},
        "issues": [str, ...], "fixes": [str, ...]}``.
    """
    issues: list[str] = []
    fixes: list[str] = []
    report: dict = {
        "terrain_faces": [faces_x, faces_z],
        "expected_heightmap_px": [faces_x + 1, faces_z + 1],
        "expected_mask_px": [faces_x, faces_z],
        "heightmap": None,
        "masks": [],
        "satellite": None,
        "issues": issues,
        "fixes": fixes,
    }

    def _log_issue(msg: str) -> None:
        issues.append(msg)
        logger.error("Raster contract: %s", msg)
        if job is not None and hasattr(job, "add_log"):
            job.add_log(f"Raster contract issue: {msg}", "warning")

    # --- heightmap.asc -------------------------------------------------------
    asc = output_dir / "heightmap.asc"
    if asc.exists():
        try:
            ncols, nrows = parse_asc_header_dims(asc)
            report["heightmap"] = {"ncols": ncols, "nrows": nrows}
            if (ncols, nrows) != (faces_x + 1, faces_z + 1):
                _log_issue(
                    f"heightmap.asc is {ncols}x{nrows}, expected "
                    f"{faces_x + 1}x{faces_z + 1} (faces+1)"
                )
        except Exception as exc:  # noqa: BLE001 - report, never abort generation
            _log_issue(f"could not read heightmap.asc header: {exc}")
    else:
        _log_issue("heightmap.asc is missing")

    # --- surface_*.png -------------------------------------------------------
    for mask_path in sorted(output_dir.glob("surface_*.png")):
        if mask_path.name == "surface_preview.png":
            continue  # RGB human-readable preview, not a paint mask
        entry = {"file": mask_path.name, "size": None, "mode": None}
        try:
            with Image.open(mask_path) as img:
                size, mode = img.size, img.mode
                entry["size"] = list(size)
                entry["mode"] = mode
                # Harden encoding: paint masks must be single-channel 8-bit "L".
                if mode != "L":
                    img.convert("L").save(str(mask_path), format="PNG")
                    entry["mode"] = "L"
                    fixes.append(f"{mask_path.name}: {mode} -> L")
                if size != (faces_x, faces_z):
                    _log_issue(
                        f"{mask_path.name} is {size[0]}x{size[1]}, expected "
                        f"{faces_x}x{faces_z} (face resolution)"
                    )
        except Exception as exc:  # noqa: BLE001
            _log_issue(f"could not read {mask_path.name}: {exc}")
        report["masks"].append(entry)

    # --- satellite_map.png ---------------------------------------------------
    sat = output_dir / "satellite_map.png"
    if sat.exists():
        entry = {"file": sat.name, "size": None, "mode": None}
        try:
            with Image.open(sat) as img:
                size, mode = img.size, img.mode
                entry["size"] = list(size)
                entry["mode"] = mode
                # Strip alpha / palette / ICC profile -> plain RGB.
                has_profile = bool(img.info.get("icc_profile"))
                if mode != "RGB" or has_profile:
                    img.convert("RGB").save(str(sat), format="PNG")
                    entry["mode"] = "RGB"
                    fixes.append(
                        f"{sat.name}: {mode}"
                        f"{'+icc' if has_profile else ''} -> RGB"
                    )
                # The satellite is stretched over the whole terrain by the
                # Terrain Tool, so its aspect ratio must match the terrain's —
                # NOT be square. Terrain may legitimately be rectangular
                # (issue #197). A mismatch means the imagery is squashed or
                # stretched on the ground, which is not fatal (Workbench
                # resamples) but is always an upstream defect.
                # Solid nodata: a lost tile in any tiled fetch composites as
                # pure black rather than failing, so check the pixels, not
                # just the header (issue found on SE_59N_14E).
                try:
                    import numpy as np

                    arr = np.asarray(img.convert("RGB"))
                    void = float(np.all(arr == 0, axis=2).mean())
                    entry["nodata_fraction"] = round(void, 6)
                    if void > SATELLITE_MAX_NODATA_FRACTION:
                        _log_issue(
                            f"satellite_map.png is {void:.1%} pure black "
                            f"(limit {SATELLITE_MAX_NODATA_FRACTION:.1%}) — "
                            f"an imagery tile is missing, leaving a hole"
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not measure satellite nodata: %s", exc)

                if size[1] > 0 and faces_z > 0:
                    want = faces_x / faces_z
                    got = size[0] / size[1]
                    if abs(got - want) > SATELLITE_ASPECT_TOLERANCE * want:
                        _log_issue(
                            f"satellite_map.png is {size[0]}x{size[1]} "
                            f"(aspect {got:.3f}), expected aspect {want:.3f} "
                            f"to match the {faces_x}x{faces_z} terrain"
                        )
        except Exception as exc:  # noqa: BLE001
            _log_issue(f"could not read satellite_map.png: {exc}")
        report["satellite"] = entry

    report["ok"] = not issues
    logger.info(
        "Raster contract: faces=%dx%d, heightmap=%s, masks=%d, satellite=%s, "
        "issues=%d, fixes=%d",
        faces_x, faces_z, report["heightmap"], len(report["masks"]),
        report["satellite"], len(issues), len(fixes),
    )
    # Spell the verdict out — a dimension mismatch here is a documented cause
    # of the first-paint crash, so it must not be buried in a stats line.
    for fix in fixes:
        logger.info("Raster contract: auto-fixed %s", fix)
    if issues:
        # Dimension mismatches are the documented crash trigger; a nodata hole
        # or a wrong aspect ratio is a visual defect, not a crash. Don't cry
        # crash for every issue — an inaccurate warning gets ignored.
        dimension_issue = any(
            "expected" in str(i) or "aspect" in str(i) for i in issues
        )
        logger.error(
            "Raster contract FAILED with %d issue(s)%s",
            len(issues),
            " — the generated project may crash the World Editor on import"
            if dimension_issue
            else " — the generated rasters have visible defects",
        )
    else:
        logger.info(
            "Raster contract PASSED — heightmap %dx%d, %d mask(s) at %dx%d, "
            "satellite %s",
            faces_x + 1, faces_z + 1, len(report["masks"]), faces_x, faces_z,
            "present" if report["satellite"] else "absent",
        )
    if job is not None and hasattr(job, "add_log"):
        job.add_log(
            f"Raster contract {'PASSED' if not issues else 'FAILED'}: "
            f"{len(report['masks'])} mask(s), {len(fixes)} auto-fix(es), "
            f"{len(issues)} issue(s)",
            "success" if not issues else "warning",
        )
    return report
