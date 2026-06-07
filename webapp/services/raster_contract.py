"""
Raster dimension/encoding contract for generated terrain inputs.

The Enfusion World Editor bakes the terrain's raster inputs (heightmap, surface
masks, satellite map) into per-block textures. A mismatched dimension or an
unexpected channel/profile is a documented cause of access-violation crashes on
the first paint stroke (issues #100/#111/#115/#138). For a terrain of ``N`` faces
per axis the contract is:

* ``heightmap.asc``   -> ``(N+1) x (N+1)``  (faces + 1 vertices)
* ``surface_*.png``   -> ``N x N``          (face resolution, 8-bit grayscale "L")
* ``satellite_map.png`` -> square, plain 8-bit ``RGB`` (no alpha / ICC profile)

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
                if size[0] != size[1]:
                    # Non-square is not fatal (Workbench resamples) but flag it.
                    _log_issue(
                        f"satellite_map.png is non-square {size[0]}x{size[1]}"
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
    return report
