FROM python:3.11-slim-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    libspatialindex-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /app

# Install Python dependencies
COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================================
# Production image
# ============================================
FROM python:3.11-slim-bookworm

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal32 \
    libgeos-c1v5 \
    libproj25 \
    libspatialindex6 \
    curl \
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
