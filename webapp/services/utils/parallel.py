"""
Multi-core parallel wrappers for CPU-bound scipy/numpy operations.

scipy.ndimage operations (gaussian_filter, zoom, binary_dilation, etc.) are
implemented as pure C loops that do NOT use BLAS/LAPACK and therefore do NOT
benefit from OMP_NUM_THREADS or OpenBLAS threading.

However, they DO release the Python GIL during execution, which means we can
run them on separate data chunks in parallel threads and achieve true
multi-core speedup.

This module provides:
- parallel_gaussian_filter: Chunked gaussian_filter across multiple cores
- parallel_zoom: Chunked zoom (cubic resampling) across multiple cores
- parallel_edt: Multi-threaded Euclidean distance transform (via `edt` package
  if available, fallback to scipy single-threaded)

Usage:
    from services.utils.parallel import parallel_gaussian_filter, parallel_edt

    smoothed = parallel_gaussian_filter(elevation, sigma=5.0)
    dist = parallel_edt(binary_mask)
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Default thread count: use all available CPUs (capped at 8 to be reasonable)
_DEFAULT_WORKERS = min(8, os.cpu_count() or 2)


# ---------------------------------------------------------------------------
# Chunked parallel gaussian_filter
# ---------------------------------------------------------------------------

def parallel_gaussian_filter(
    image: np.ndarray,
    sigma: float,
    workers: Optional[int] = None,
) -> np.ndarray:
    """
    Apply scipy.ndimage.gaussian_filter in parallel using chunked threading.

    Splits the array along axis 0, adds overlap (halo) regions to avoid
    seam artifacts, runs gaussian_filter on each chunk in a separate thread,
    then reassembles the result.

    Args:
        image: 2D input array (float32 or float64).
        sigma: Gaussian sigma in pixels.
        workers: Number of threads (default: CPU count, capped at 8).

    Returns:
        Filtered array, same shape and dtype as input.
    """
    from scipy.ndimage import gaussian_filter

    if workers is None:
        workers = _DEFAULT_WORKERS

    rows = image.shape[0]

    # For small arrays or single worker, just use scipy directly
    if workers <= 1 or rows < workers * 4:
        return gaussian_filter(image, sigma=sigma)

    # Overlap must be large enough to avoid seam artifacts (4*sigma is standard)
    overlap = max(1, int(np.ceil(4 * sigma)))

    # Split into roughly equal chunks
    chunk_starts = np.linspace(0, rows, workers + 1, dtype=int)

    def _process_chunk(idx: int) -> tuple[int, int, np.ndarray]:
        """Process one chunk with halo overlap."""
        start = chunk_starts[idx]
        end = chunk_starts[idx + 1]

        # Extend with overlap
        padded_start = max(0, start - overlap)
        padded_end = min(rows, end + overlap)

        chunk = image[padded_start:padded_end]
        filtered = gaussian_filter(chunk, sigma=sigma)

        # Trim overlap back to the original chunk region
        trim_start = start - padded_start
        trim_end = trim_start + (end - start)

        return start, end, filtered[trim_start:trim_end]

    # Run all chunks in parallel (scipy releases the GIL)
    result = np.empty_like(image)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_chunk, i) for i in range(workers)]
        for future in futures:
            start, end, chunk_result = future.result()
            result[start:end] = chunk_result

    return result


# ---------------------------------------------------------------------------
# Chunked parallel zoom
# ---------------------------------------------------------------------------

def parallel_zoom(
    image: np.ndarray,
    zoom_factors: tuple[float, float] | float,
    order: int = 3,
    workers: Optional[int] = None,
) -> np.ndarray:
    """
    Apply scipy.ndimage.zoom in parallel using chunked threading.

    Splits the array along axis 0, adds overlap for the interpolation
    support region, runs zoom on each chunk, then reassembles.

    Args:
        image: 2D input array.
        zoom_factors: Zoom factor(s) — scalar or (zoom_y, zoom_x).
        order: Spline order (3 = cubic).
        workers: Number of threads.

    Returns:
        Zoomed array.
    """
    from scipy.ndimage import zoom

    if workers is None:
        workers = _DEFAULT_WORKERS

    if isinstance(zoom_factors, (int, float)):
        zoom_y = zoom_x = float(zoom_factors)
    else:
        zoom_y, zoom_x = zoom_factors

    rows = image.shape[0]

    # For small arrays or single worker, just use scipy directly
    if workers <= 1 or rows < workers * 4:
        return zoom(image, (zoom_y, zoom_x), order=order)

    # Overlap for cubic spline needs ~order+1 pixels
    overlap = order + 2

    # Pre-compute the exact expected output height so that rounding errors
    # across chunks don't accumulate into extra/missing rows.
    target_rows = int(round(rows * zoom_y))

    chunk_starts = np.linspace(0, rows, workers + 1, dtype=int)

    # Pre-compute each chunk's target output rows from the known total.
    # This avoids the rounding drift that caused non-square heightmaps
    # (e.g. 2054×2049 instead of 2049×2049).
    out_starts = [int(round(chunk_starts[i] * zoom_y)) for i in range(workers + 1)]

    def _process_chunk(idx: int) -> tuple[int, np.ndarray]:
        start = chunk_starts[idx]
        end = chunk_starts[idx + 1]
        expected_out_rows = out_starts[idx + 1] - out_starts[idx]

        padded_start = max(0, start - overlap)
        padded_end = min(rows, end + overlap)

        chunk = image[padded_start:padded_end]
        zoomed = zoom(chunk, (zoom_y, zoom_x), order=order)

        # Calculate where the original chunk maps to in the zoomed output
        total_padded = padded_end - padded_start
        zoomed_total = zoomed.shape[0]

        # Proportional mapping: trim_start/trim_end in zoomed space
        frac_start = (start - padded_start) / total_padded
        frac_end = (end - padded_start) / total_padded
        trim_start = int(round(frac_start * zoomed_total))
        trim_end = trim_start + expected_out_rows

        # Clamp to actual array bounds
        trim_end = min(trim_end, zoomed_total)

        return idx, zoomed[trim_start:trim_end]

    # Run chunks in parallel
    chunks_out = [None] * workers
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_chunk, i) for i in range(workers)]
        for future in futures:
            idx, chunk_result = future.result()
            chunks_out[idx] = chunk_result

    result = np.concatenate(chunks_out, axis=0)

    # Final safety trim/pad to guarantee exact target dimensions
    if result.shape[0] > target_rows:
        result = result[:target_rows]
    elif result.shape[0] < target_rows:
        # Extremely unlikely after the pre-computed splits, but pad if needed
        pad_rows = target_rows - result.shape[0]
        result = np.pad(result, ((0, pad_rows), (0, 0)), mode="edge")

    return result


# ---------------------------------------------------------------------------
# Multi-threaded Euclidean Distance Transform
# ---------------------------------------------------------------------------

def parallel_edt(
    binary_mask: np.ndarray,
    workers: Optional[int] = None,
) -> np.ndarray:
    """
    Multi-threaded Euclidean Distance Transform.

    Uses the `edt` package (pip install edt) if available, which provides
    a native multi-threaded EDT implementation. Falls back to scipy's
    single-threaded distance_transform_edt.

    Args:
        binary_mask: Boolean or uint8 array (non-zero = inside).
        workers: Number of threads for the `edt` package.

    Returns:
        Float array with Euclidean distances.
    """
    if workers is None:
        workers = _DEFAULT_WORKERS

    try:
        import edt as edt_pkg
        # The edt package accepts bool/uint8 and returns float32 by default
        result = edt_pkg.edt(
            binary_mask.astype(np.uint8, copy=False),
            parallel=workers,
        )
        return result
    except ImportError:
        # Fallback to scipy single-threaded EDT
        from scipy.ndimage import distance_transform_edt
        return distance_transform_edt(binary_mask)


# ---------------------------------------------------------------------------
# GDAL multi-threading for rasterio operations
# ---------------------------------------------------------------------------

def configure_gdal_threading():
    """
    Configure GDAL to use multiple threads for warp/reproject operations.

    Call this once at application startup. Sets GDAL_NUM_THREADS which
    enables multi-threaded warping in rasterio.warp.reproject().
    """
    num_threads = os.cpu_count() or 2
    os.environ.setdefault("GDAL_NUM_THREADS", str(num_threads))
    logger.info(f"GDAL threading: GDAL_NUM_THREADS={os.environ['GDAL_NUM_THREADS']}")
