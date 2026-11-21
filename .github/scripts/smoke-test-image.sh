#!/usr/bin/env bash
#
# Exercise a freshly built image, not just the fact that it built.
#
# The Dockerfile installs no GIS libraries: rasterio, pyproj and shapely each
# vendor their own GDAL, PROJ and GEOS inside their wheels. "It imports" does
# not prove that - a vendored GDAL with missing data files fails only when it
# is asked to do a CRS-aware warp. So this does the real work: a reprojection
# into the pipeline's own projection, a GeoTIFF round-trip, a GEOS buffer, a
# Pillow resample, and the HTTP health endpoint from a running container.
#
# It also asserts that `edt` is importable and still accepts black_border=.
# That is not cosmetic: issue #202 was an edge artifact that appeared only when
# `edt` was absent or too old, and it was invisible on hosts without it.
#
# It also asserts that the Python packaging tooling is gone. The production
# stage deletes pip, setuptools, wheel and pkg_resources, because every open
# security finding on this repository lived in them and nothing in the image
# needs them. That deletion is only worth anything if it keeps happening, so
# it is checked on the finished image rather than trusted to the Dockerfile.
#
# Usage: smoke-test-image.sh <image-ref>
set -euo pipefail

IMAGE="${1:?usage: smoke-test-image.sh <image-ref>}"

echo "::group::Vendored library versions"
docker run --rm -i --entrypoint python "$IMAGE" - <<'PY'
import PIL, fastapi, numpy, pyproj, rasterio, scipy, shapely, starlette
print("rasterio", rasterio.__version__, "-> GDAL", rasterio.__gdal_version__)
print("pyproj  ", pyproj.__version__, "-> PROJ", pyproj.proj_version_str)
print("shapely ", shapely.__version__, "-> GEOS", shapely.geos_version_string)
print("Pillow  ", PIL.__version__)
print("numpy   ", numpy.__version__, "| scipy", scipy.__version__)
print("fastapi ", fastapi.__version__, "| starlette", starlette.__version__)
PY
echo "::endgroup::"

echo "::group::Real GIS work with no system GDAL, PROJ or GEOS"
docker run --rm -i --entrypoint python "$IMAGE" - <<'PY'
import numpy as np
import pyproj
import rasterio
import shapely.geometry as sg
from PIL import Image
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject

# A CRS-aware warp into SWEREF99 TM, which is what the Swedish pipeline uses.
# This is the step that needs GDAL's and PROJ's data files, so an incomplete
# vendored copy fails here rather than silently later.
src_crs, dst_crs = "EPSG:4326", "EPSG:3006"
src = np.linspace(0, 1, 64 * 64, dtype="float32").reshape(64, 64)
src_transform = from_origin(13.0, 58.0, 0.01, 0.01)
dst_transform, dst_w, dst_h = calculate_default_transform(
    src_crs, dst_crs, 64, 64, 13.0, 57.36, 13.64, 58.0
)
dst = np.zeros((dst_h, dst_w), dtype="float32")
reproject(
    source=src, destination=dst,
    src_transform=src_transform, src_crs=src_crs,
    dst_transform=dst_transform, dst_crs=dst_crs,
    resampling=Resampling.bilinear,
)
assert np.isfinite(dst).any() and dst.any(), "warp produced no data"
print(f"warp 4326 -> 3006 ok: {dst.shape}")

t = pyproj.Transformer.from_crs(4326, 3006, always_xy=True)
e, n = t.transform(13.0, 58.0)
assert 300_000 < e < 500_000 and 6_000_000 < n < 7_000_000, (e, n)
print(f"pyproj 4326 -> 3006 ok: {e:.0f}, {n:.0f}")

poly = sg.Polygon([(0, 0), (3, 0), (3, 3), (0, 3)]).buffer(0.5).simplify(0.1)
assert poly.is_valid and poly.area > 9, poly.area
print(f"shapely buffer + simplify ok: area {poly.area:.3f}")

with MemoryFile() as mem:
    with mem.open(driver="GTiff", width=64, height=64, count=1,
                  dtype="float32", crs=dst_crs, transform=dst_transform) as ds:
        ds.write(src, 1)
    with mem.open() as ds:
        assert ds.read(1).shape == (64, 64)
        assert ds.crs.to_epsg() == 3006, ds.crs
print("GTiff write + read back ok, CRS preserved")

out = Image.fromarray((src * 255).astype("uint8")).resize((32, 32), Image.LANCZOS)
assert out.size == (32, 32)
print("Pillow LANCZOS resample ok")

# issue #202: the edge artifact appeared when `edt` was missing or too old to
# take black_border=. Both must hold in the image.
import edt
d = edt.edt(np.ones((8, 8), dtype="uint8"), parallel=1, black_border=False)
assert d.shape == (8, 8), d.shape
print(f"edt with black_border=False ok: {edt.__version__ if hasattr(edt, '__version__') else 'installed'}")
PY
echo "::endgroup::"

echo "::group::No Python packaging tooling in the runtime image"
# The Dockerfile deletes pip, setuptools, wheel and pkg_resources from the
# production stage: they are build-time tools, and every open security finding
# on this repository came from them (CVE-2026-23949 and CVE-2026-24049 in
# setuptools/_vendor, six more against pip 24.0 and setuptools 79.0.1).
#
# The Dockerfile asserts this at build time, which catches a base image that
# hides the tooling somewhere else. This asserts it on the finished image,
# which catches a later layer putting it back - and checks the console scripts
# as well as the importable packages, since a stale /usr/local/bin/pip would
# still be a working installer.
docker run --rm --entrypoint python "$IMAGE" -c "
import importlib.util, os

left = [m for m in ('pip', 'setuptools', 'pkg_resources', 'wheel')
        if importlib.util.find_spec(m) is not None]
assert not left, 'packaging tooling importable in the image: %s' % left

scripts = sorted(
    f for f in os.listdir('/usr/local/bin')
    if f.startswith(('pip', 'wheel', 'easy_install'))
)
assert not scripts, 'packaging console scripts left in the image: %s' % scripts

print('pip, setuptools, wheel and pkg_resources are all absent')
"
# A dangling .pth would make every interpreter start print a traceback to
# stderr while still exiting 0, so check the stream, not the exit code.
noise="$(docker run --rm --entrypoint python "$IMAGE" -c 'pass' 2>&1)"
if [ -n "$noise" ]; then
  echo "interpreter start is not silent: $noise" >&2
  exit 1
fi
echo "interpreter starts clean - no orphaned distutils-precedence.pth"
echo "::endgroup::"

echo "::group::Container starts and answers /api/health"
cid="$(docker run -d -p 18080:8080 "$IMAGE")"
cleanup() {
  echo "--- container logs ---"
  docker logs "$cid" 2>&1 | tail -40 || true
  docker rm -f "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  if body="$(curl -fsS http://127.0.0.1:18080/api/health)"; then
    echo "$body"
    case "$body" in
      *'"status":"healthy"'*) echo "health ok"; exit 0 ;;
      *) echo "health endpoint answered but not healthy" >&2; exit 1 ;;
    esac
  fi
  sleep 2
done

echo "health endpoint never answered within 60s" >&2
exit 1
