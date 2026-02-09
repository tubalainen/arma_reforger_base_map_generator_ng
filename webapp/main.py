"""
Arma Reforger Base Map Generator - Web Application

FastAPI backend that serves the web UI and handles map generation requests.
Uses open GIS APIs (OpenTopography, Overpass/OSM, Kartverket, etc.) to
fetch elevation, road, water, forest, and building data for any selected area.
"""

import asyncio
import logging
import os
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import OUTPUT_DIR
from middleware.session import SessionMiddleware
from middleware.security import SecurityHeadersMiddleware
from middleware.rate_limit import RateLimitMiddleware
from services.validators import validate_job_id, validate_image_type, validate_polygon
from services.utils.parallel import configure_gdal_threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")

# Enable multi-threaded GDAL for rasterio.warp.reproject operations
configure_gdal_threading()

# ===========================================================================
# Application version (for cache busting)
# ===========================================================================

APP_VERSION = "1.0.1"  # Increment when static files change

# ===========================================================================
# FastAPI app
# ===========================================================================

app = FastAPI(
    title="Arma Reforger Base Map Generator",
    description="Generate base maps for Arma Reforger from real-world open GIS data",
    version=APP_VERSION,
)

# ===========================================================================
# Middleware (order matters - first added = outermost)
# ===========================================================================

# CORS configuration
# Configure allowed origins from environment or use defaults
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Rate limiting
app.add_middleware(RateLimitMiddleware)

# Session management (innermost - runs first on request)
app.add_middleware(SessionMiddleware)

# ===========================================================================
# Startup event handlers
# ===========================================================================


@app.on_event("startup")
async def startup_event():
    """
    Application startup tasks.

    Clears any hanging sessions from previous runs to ensure a clean state.
    """
    from services.session_service import clear_all_sessions

    logger.info("Application starting up...")
    cleared_count = clear_all_sessions()
    logger.info(f"Startup cleanup: cleared {cleared_count} hanging sessions")


# ===========================================================================
# Cleanup scheduler
# ===========================================================================

# Constants for file retention
FILE_RETENTION_MINUTES = 10

# Track scheduled cleanups: {job_id: cleanup_time} - thread-safe access required
scheduled_cleanups: dict[str, datetime] = {}
_cleanups_lock = threading.RLock()


async def schedule_cleanup(job_id: str, delay_minutes: int = FILE_RETENTION_MINUTES):
    """
    Schedule automatic cleanup of job files after a delay.

    Args:
        job_id: Job identifier
        delay_minutes: Delay in minutes before cleanup (default: 10)
    """
    cleanup_time = datetime.now() + timedelta(minutes=delay_minutes)

    with _cleanups_lock:
        scheduled_cleanups[job_id] = cleanup_time

    logger.info(f"Scheduled cleanup for job {job_id[:8]}... at {cleanup_time}")

    # Wait for the specified delay
    await asyncio.sleep(delay_minutes * 60)

    # Perform cleanup
    try:
        from services.export_service import cleanup_job
        from services.map_generator import is_download_active

        # Check if download is currently active
        if is_download_active(job_id):
            logger.info(f"Skipping cleanup for job {job_id[:8]}... (download in progress)")
            # Reschedule for 5 minutes later
            await asyncio.sleep(300)  # 5 minutes

        # Verify the job directory and files still exist before cleanup
        job_dir = OUTPUT_DIR / job_id
        zip_path = OUTPUT_DIR / f"map_{job_id}.zip"

        if job_dir.exists() or zip_path.exists():
            cleanup_job(job_id, OUTPUT_DIR)
            logger.info(f"Auto-cleanup completed for job {job_id[:8]}...")
        else:
            logger.debug(f"Job {job_id[:8]}... already cleaned up")

        # Remove from scheduled cleanups
        with _cleanups_lock:
            scheduled_cleanups.pop(job_id, None)
    except Exception as e:
        logger.error(f"Auto-cleanup failed for job {job_id[:8]}...: {e}")


# ===========================================================================
# Helper functions
# ===========================================================================


def get_session_job(request: Request, job_id: str):
    """
    Get a job and verify session ownership.

    Args:
        request: The incoming request (must have session in state)
        job_id: The job ID to retrieve

    Returns:
        The MapGenerationJob if found and owned by session

    Raises:
        HTTPException: If job not found or not owned by session
    """
    from services.map_generator import get_job

    # Validate job ID format
    validate_job_id(job_id)

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found")

    # Verify session ownership
    session = request.state.session
    if job.session_id != session.session_id:
        logger.warning(
            f"Access denied: session {session.session_id[:8]}... "
            f"tried to access job {job_id[:8]}... owned by {job.session_id[:8]}..."
        )
        raise HTTPException(status_code=403, detail="Access denied")

    return job


# ===========================================================================
# Request/Response models
# ===========================================================================


class DetectCountriesRequest(BaseModel):
    polygon: list[list[float]]  # [[lng, lat], ...]


class GenerateRequest(BaseModel):
    polygon: list[list[float]]  # [[lng, lat], ...]
    options: dict = {}
    map_name: Optional[str] = ""  # Enfusion project name (letters, numbers, underscores)


# ===========================================================================
# Routes - Pages
# ===========================================================================


@app.get("/")
async def index():
    """Serve the main web application page with cache-busting version."""
    # Read the HTML file and inject the version for cache busting
    with open("static/index.html", "r") as f:
        html_content = f.read()

    # Replace static file URLs with versioned URLs
    html_content = html_content.replace(
        'href="/static/css/style.css"',
        f'href="/static/css/style.css?v={APP_VERSION}"'
    )
    html_content = html_content.replace(
        'src="/static/js/app.js"',
        f'src="/static/js/app.js?v={APP_VERSION}"'
    )

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html_content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })


@app.get("/favicon.ico")
async def favicon():
    """Serve the favicon."""
    favicon_path = Path("static/favicon.ico")
    if favicon_path.exists():
        return FileResponse("static/favicon.ico", media_type="image/x-icon")
    # Return a 204 No Content if favicon doesn't exist to avoid 404 errors
    from fastapi.responses import Response

    return Response(status_code=204)


@app.get("/static/js/app.js")
async def serve_app_js():
    """Serve JavaScript with no-cache headers to prevent stale cached versions."""
    return FileResponse(
        "static/js/app.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/static/css/style.css")
async def serve_style_css():
    """Serve CSS with no-cache headers to prevent stale cached versions."""
    return FileResponse(
        "static/css/style.css",
        media_type="text/css",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


# ===========================================================================
# Routes - API
# ===========================================================================


@app.post("/api/detect-countries")
async def detect_countries(request_body: DetectCountriesRequest):
    """
    Detect which countries a polygon covers.
    Uses Nominatim reverse geocoding with bounding box pre-filtering.
    """
    from services.country_detector import detect_countries as _detect

    try:
        # Validate polygon
        validate_polygon(request_body.polygon)
        result = await _detect(request_body.polygon)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Country detection failed: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Country detection failed: {str(e)}"},
        )


@app.post("/api/analyze")
async def analyze_area(request_body: DetectCountriesRequest):
    """
    Analyze a polygon area: detect countries and return available data sources.
    """
    from services.country_detector import (
        detect_countries as _detect,
        get_data_sources_for_country,
    )

    try:
        # Validate polygon
        validate_polygon(request_body.polygon)
        result = await _detect(request_body.polygon)
        sources = {}
        for cc in result.get("countries", []):
            sources[cc] = get_data_sources_for_country(cc)
        result["data_sources"] = sources
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Area analysis failed: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Area analysis failed: {str(e)}"},
        )


@app.get("/api/data-sources")
async def get_data_sources():
    """Return status of all available data sources."""
    from config import ELEVATION_CONFIGS, COUNTRY_NAMES

    sources = {
        "global": {
            "overpass_api": {
                "status": "available",
                "description": "OpenStreetMap via Overpass API",
            },
            "opentopography_cop30": {
                "status": "available",
                "description": "Copernicus DEM 30m (OpenTopography)",
            },
            "sentinel2_cloudless": {
                "status": "available",
                "description": "Sentinel-2 Cloudless (EOX)",
            },
        },
        "countries": {},
    }

    for code, cfg in ELEVATION_CONFIGS.items():
        status = "available"
        note = ""
        if cfg.auth_type == "none":
            status = "available"
        elif cfg.auth_type in ("token", "api_key"):
            if os.environ.get(cfg.auth_env_var, ""):
                status = "configured"
            else:
                status = "api_key_required"
                note = f"Set {cfg.auth_env_var}"
        elif cfg.auth_type == "oauth2":
            status = "not_implemented"
            note = "OAuth2 not yet supported"

        sources["countries"][code] = {
            "name": COUNTRY_NAMES.get(code, cfg.name),
            "elevation_source": cfg.name,
            "resolution_m": cfg.resolution_m,
            "status": status,
            "note": note,
        }

    return sources


@app.post("/api/generate")
async def start_generation(
    request: Request, request_body: GenerateRequest, background_tasks: BackgroundTasks
):
    """
    Start a map generation job.

    Validates the polygon, creates a job, and starts background processing.
    Returns immediately with a job ID and access token for polling/downloading.
    """
    from services.map_generator import create_job, run_generation
    from services.session_service import generate_access_token

    # Validate polygon
    validate_polygon(request_body.polygon)

    # Get session from request state
    session = request.state.session

    # Merge map_name into options so the pipeline can access it
    generation_options = dict(request_body.options)
    if request_body.map_name:
        # Sanitize: only allow alphanumerics and underscores, max 32 chars
        import re
        sanitized = re.sub(r'[^A-Za-z0-9_]', '', request_body.map_name)[:32]
        if sanitized:
            generation_options["map_name"] = sanitized

    # Create and start job (with session association)
    job = create_job(request_body.polygon, generation_options, session.session_id)

    # Register job with session
    session.add_job(job.job_id)

    # Generate access token for this job (for downloads without cookies)
    access_token = generate_access_token(job.job_id, session.session_id)

    map_label = generation_options.get("map_name", "auto")
    logger.info(
        f"Created job {job.job_id[:8]}... for polygon with {len(request_body.polygon)} vertices "
        f"(map_name={map_label})"
    )

    # Run generation in background
    # Note: We don't schedule cleanup here anymore - it's now scheduled
    # when generation completes to give users the full retention time
    background_tasks.add_task(run_generation, job)

    return {
        "job_id": job.job_id,
        "access_token": access_token,
        "status": "pending",
        "message": "Generation started",
    }


@app.get("/status/{job_id}")
async def get_job_status_public(job_id: str):
    """
    Get the current status and progress of a generation job (PUBLIC endpoint).

    This endpoint is accessible via Cloudflare tunnel and does NOT require
    authentication. It's used for polling job progress from the frontend.
    """
    from services.map_generator import get_job

    # Validate job ID format
    validate_job_id(job_id)

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_data = job.to_dict()

    # Add retention information if cleanup is scheduled
    with _cleanups_lock:
        if job_id in scheduled_cleanups:
            cleanup_time = scheduled_cleanups[job_id]
            time_remaining = (cleanup_time - datetime.now()).total_seconds() / 60
            job_data["retention"] = {
                "cleanup_scheduled": True,
                "cleanup_time": cleanup_time.isoformat(),
                "minutes_remaining": max(0, int(time_remaining)),
            }

    return job_data


@app.get("/api/job/{job_id}")
async def get_job_status_protected(request: Request, job_id: str):
    """
    Get the current status and progress of a generation job (PROTECTED endpoint).

    This endpoint requires session cookie authentication.
    NOT accessible via Cloudflare tunnel (blocked for security).
    """
    # Verify session ownership
    job = get_session_job(request, job_id)

    job_data = job.to_dict()

    # Add retention information if cleanup is scheduled
    with _cleanups_lock:
        if job_id in scheduled_cleanups:
            cleanup_time = scheduled_cleanups[job_id]
            time_remaining = (cleanup_time - datetime.now()).total_seconds() / 60
            job_data["retention"] = {
                "cleanup_scheduled": True,
                "cleanup_time": cleanup_time.isoformat(),
                "minutes_remaining": max(0, int(time_remaining)),
            }

    return job_data


@app.get("/download/{job_id}")
async def download_result(request: Request, job_id: str, token: Optional[str] = None):
    """
    Download the generated map package as a ZIP file (PUBLIC endpoint).

    Supports two authentication methods:
    1. Session cookie (for same-browser downloads)
    2. Access token via query parameter (for Cloudflare tunnel access)

    This endpoint is accessible via Cloudflare tunnel when using token authentication.
    """
    from services.export_service import get_output_zip
    from services.map_generator import get_job, mark_download_active, mark_download_complete
    from services.session_service import verify_access_token

    # Validate job ID format
    validate_job_id(job_id)

    # Check if job exists
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Verify authorization via session cookie OR access token
    authorized = False
    auth_method = None

    # Try session cookie first (if available)
    if request.state.session and request.state.session.owns_job(job_id):
        authorized = True
        auth_method = "session"
        logger.debug(f"Download authorized via session for job {job_id[:8]}...")

    # Try access token if session didn't work
    if not authorized and token:
        if verify_access_token(token, job_id):
            authorized = True
            auth_method = "token"
            logger.debug(f"Download authorized via token for job {job_id[:8]}...")

    if not authorized:
        logger.warning(f"Download unauthorized for job {job_id[:8]}...")
        raise HTTPException(
            status_code=403,
            detail="Access denied. Provide a valid session cookie or access token.",
        )

    # Mark download as active to prevent cleanup
    mark_download_active(job_id)

    try:
        zip_path = get_output_zip(job_id, OUTPUT_DIR)
        if not zip_path:
            mark_download_complete(job_id)
            raise HTTPException(
                status_code=404,
                detail="Output file not found. Generation may still be in progress.",
            )

        # Calculate retention time remaining
        retention_msg = (
            f"This file will be automatically deleted {FILE_RETENTION_MINUTES} minutes after generation."
        )
        with _cleanups_lock:
            if job_id in scheduled_cleanups:
                cleanup_time = scheduled_cleanups[job_id]
                time_remaining = (cleanup_time - datetime.now()).total_seconds() / 60
                if time_remaining > 0:
                    retention_msg = f"This file will be automatically deleted in approximately {int(time_remaining)} minutes."

        logger.info(f"Download started for job {job_id[:8]}... via {auth_method}")

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=f"arma_reforger_map_{job_id[:16]}.zip",
            headers={
                "X-File-Retention": retention_msg,
            },
            background=BackgroundTasks().add_task(mark_download_complete, job_id),
        )
    except HTTPException:
        mark_download_complete(job_id)
        raise
    except Exception as e:
        mark_download_complete(job_id)
        logger.error(f"Download failed for job {job_id[:8]}...: {e}")
        raise HTTPException(status_code=500, detail="Download failed")


@app.get("/job/{job_id}")
async def download_result_legacy(request: Request, job_id: str):
    """
    Legacy download endpoint for backward compatibility.

    Redirects to /download/{job_id} with session-based authentication.
    """
    # Verify session ownership
    job = get_session_job(request, job_id)

    # Redirect to new endpoint
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/download/{job_id}", status_code=307)


@app.get("/api/job/{job_id}/preview/{image_type}")
async def get_preview(request: Request, job_id: str, image_type: str):
    """
    Get a preview image (heightmap or surface) for a completed job.

    Requires session ownership verification for proper multi-user isolation.
    """
    from services.export_service import get_preview_image

    # Verify session ownership
    job = get_session_job(request, job_id)

    # Validate image type
    image_type = validate_image_type(image_type)

    image_path = get_preview_image(job_id, OUTPUT_DIR, image_type)
    if not image_path:
        raise HTTPException(status_code=404, detail="Preview image not found")
    return FileResponse(path=str(image_path), media_type="image/png")


@app.get("/api/job/{job_id}/files")
async def list_job_files(request: Request, job_id: str):
    """
    List all output files for a job.

    Requires session ownership verification for proper multi-user isolation.
    """
    from services.export_service import list_job_files as _list

    # Verify session ownership
    job = get_session_job(request, job_id)

    files = _list(job_id, OUTPUT_DIR)
    if not files:
        raise HTTPException(status_code=404, detail="Job output not found")
    return {"files": files}


@app.delete("/api/job/{job_id}")
async def delete_job(request: Request, job_id: str):
    """Clean up job output files."""
    from services.export_service import cleanup_job

    # Verify session ownership
    job = get_session_job(request, job_id)

    cleanup_job(job_id, OUTPUT_DIR)

    # Remove from session's job list
    session = request.state.session
    session.remove_job(job_id)

    return {"message": f"Job cleaned up"}


# ===========================================================================
# Health check
# ===========================================================================


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    from services.session_service import get_session_stats

    return {
        "status": "healthy",
        "service": "Arma Reforger Base Map Generator",
        "version": APP_VERSION,
        "sessions": get_session_stats(),
    }


# ===========================================================================
# Static files
# ===========================================================================
# Mount static files directory for serving images and other assets
# This is placed AFTER explicit routes so that /static/js/app.js and
# /static/css/style.css routes with no-cache headers take precedence
app.mount("/static", StaticFiles(directory="static"), name="static")
