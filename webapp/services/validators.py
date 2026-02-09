"""
Input validation utilities for security.

Validates and sanitizes user input to prevent path traversal,
injection attacks, and other security issues.
"""

import re
from fastapi import HTTPException

# Job IDs are URL-safe base64, 16-32 characters
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Image types that can be requested for preview
ALLOWED_IMAGE_TYPES = {"heightmap", "surface", "satellite"}


def validate_job_id(job_id: str) -> str:
    """
    Validate job ID format to prevent path traversal and injection.

    Args:
        job_id: The job ID to validate

    Returns:
        The validated job ID

    Raises:
        HTTPException: If the job ID is invalid
    """
    if not job_id:
        raise HTTPException(status_code=400, detail="Job ID is required")

    if not JOB_ID_PATTERN.match(job_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid job ID format",
        )

    # Additional check for path traversal attempts
    if ".." in job_id or "/" in job_id or "\\" in job_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid job ID format",
        )

    return job_id


def validate_image_type(image_type: str) -> str:
    """
    Validate image type to prevent arbitrary file access.

    Args:
        image_type: The image type to validate

    Returns:
        The validated image type

    Raises:
        HTTPException: If the image type is invalid
    """
    if not image_type:
        raise HTTPException(status_code=400, detail="Image type is required")

    image_type = image_type.lower().strip()

    if image_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image type. Allowed: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}",
        )

    return image_type


def validate_polygon(polygon: list[list[float]]) -> list[list[float]]:
    """
    Validate polygon coordinates.

    Args:
        polygon: List of [lng, lat] coordinate pairs

    Returns:
        The validated polygon

    Raises:
        HTTPException: If the polygon is invalid
    """
    if not polygon:
        raise HTTPException(status_code=400, detail="Polygon is required")

    if len(polygon) < 4:
        raise HTTPException(
            status_code=400,
            detail="Polygon must have at least 3 vertices (4 points including closing point)",
        )

    # Validate each coordinate
    for i, coord in enumerate(polygon):
        if not isinstance(coord, (list, tuple)) or len(coord) != 2:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid coordinate at position {i}: expected [lng, lat]",
            )

        try:
            lng, lat = float(coord[0]), float(coord[1])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid coordinate values at position {i}",
            )

        if not (-180 <= lng <= 180):
            raise HTTPException(
                status_code=400,
                detail=f"Longitude out of range at position {i}: {lng}",
            )

        if not (-90 <= lat <= 90):
            raise HTTPException(
                status_code=400,
                detail=f"Latitude out of range at position {i}: {lat}",
            )

    # Validate bounding box size
    lngs = [c[0] for c in polygon]
    lats = [c[1] for c in polygon]
    lng_range = max(lngs) - min(lngs)
    lat_range = max(lats) - min(lats)

    if lng_range > 2.0 or lat_range > 2.0:
        raise HTTPException(
            status_code=400,
            detail="Selected area is too large. Please select an area smaller than ~200km x 200km.",
        )

    if lng_range < 0.001 or lat_range < 0.001:
        raise HTTPException(
            status_code=400,
            detail="Selected area is too small. Please select a larger area.",
        )

    return polygon
