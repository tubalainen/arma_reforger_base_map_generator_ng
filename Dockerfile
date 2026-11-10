FROM python:3.11-slim-bookworm AS builder

# No build dependencies, and no apt layer at all.
#
# Every line in requirements.txt resolves to a manylinux wheel for this image's
# platform, so nothing is compiled here: the GIS wheels (rasterio, pyproj,
# shapely) vendor their own GDAL, PROJ and GEOS, and the GDAL Python bindings
# that once needed libgdal-dev are gone. If a dependency is ever added that has
# no wheel, pip will fail loudly here rather than silently build something, and
# the headers it needs belong in this stage only.
#
# The build is verified: docker-publish.yml loads the image on every pull
# request and exercises a real reprojection, a real geometry op and the health
# endpoint, which is what proves the vendored libraries are self-contained.

WORKDIR /app

# Install Python dependencies
COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================================
# Production image
# ============================================
FROM python:3.11-slim-bookworm

# Install runtime dependencies only.
#
# No GIS libraries here. rasterio, pyproj and shapely each vendor their own
# copy of GDAL, PROJ and GEOS inside their wheels, so the Debian libgdal32 /
# libproj25 / libgeos-c1v5 packages were never loaded, and libspatialindex6 was
# for rtree, which this project does not use (shapely 2's STRtree is built in).
# gdal-bin went with them: nothing shells out to the GDAL CLI.
#
# curl is load-bearing: entrypoint.sh fetches the Leaflet icons with it and the
# HEALTHCHECK calls it.
#
# osmium-tool and pbzip2 are for the Overpass sidecar's init step, not the web
# app. The importer needs bzip2 XML and the mirrors publish PBF, so something
# has to convert; doing it in this image rather than in the Overpass one is
# what lets it use every core. The Overpass image has bzip2 but no parallel
# implementation, so converting there burns an hour on a single thread while
# the rest of the machine idles.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    osmium-tool \
    pbzip2 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Enable multi-threaded processing for CPU-bound operations:
# - GDAL_NUM_THREADS: rasterio.warp.reproject uses GDAL's internal thread pool
# - OMP_NUM_THREADS: OpenBLAS linear algebra (numpy matrix ops)
# - OPENBLAS_NUM_THREADS: Same, OpenBLAS-specific override
# Note: scipy.ndimage operations (gaussian_filter, zoom, etc.) do NOT use
# BLAS threading — they are parallelized via chunked ThreadPoolExecutor in
# services/utils/parallel.py instead.
ENV GDAL_NUM_THREADS=ALL_CPUS
ENV OMP_NUM_THREADS=4
ENV OPENBLAS_NUM_THREADS=4

# Copy application code
COPY --chown=appuser:appgroup webapp/ .

# Create directories with correct permissions
RUN mkdir -p /app/output /app/static/images && chown -R appuser:appgroup /app/output /app/static/images

# Make entrypoint script executable
RUN chmod +x /app/entrypoint.sh

# Switch to non-root user
USER appuser

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# Use entrypoint script to download icons before starting
ENTRYPOINT ["/app/entrypoint.sh"]

# Production command (no --reload, with proxy headers support)
# Using 1 worker because sessions are stored in-memory (not shared across workers)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
