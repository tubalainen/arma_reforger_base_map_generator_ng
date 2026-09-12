"""
Coordinate transformation between WGS84 and Enfusion local coordinate systems.

Enfusion uses a flat Cartesian coordinate system in metres with origin (0, 0, 0)
at the terrain's lower-left (south-west) corner:
  - +X = East
  - +Z = North
  - +Y = Up (elevation, handled separately)

This module converts WGS84 geographic coordinates (lon/lat in degrees) to
Enfusion local metres and vice-versa using pyproj for accurate projection.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class CoordinateTransformer:
    """
    Transforms coordinates between WGS84 and Enfusion local coordinate systems.

    The terrain bounding box defines the mapping:
    - SW corner of bbox -> Enfusion origin (0, 0)
    - NE corner of bbox -> (terrain_width_m, terrain_depth_m)

    Uses pyproj for high-precision projection when a country-specific CRS is
    available. Falls back to a simplified equirectangular approximation when
    pyproj is unavailable or CRS is not specified.
    """

    def __init__(
        self,
        bbox: dict,
        crs: str = "EPSG:4326",
        terrain_size_m: Optional[tuple[float, float]] = None,
    ):
        """
        Initialize the coordinate transformer.

        Args:
            bbox: Bounding box with keys: west, south, east, north (WGS84 degrees)
            crs: Target CRS for projection (e.g., "EPSG:25833" for Norway).
                 If "EPSG:4326" or empty, uses equirectangular fallback.
            terrain_size_m: Optional (width_m, depth_m) of the terrain. If provided,
                            coordinates are scaled to fit exactly within these dimensions.
                            If None, uses true projected distances.
        """
        self.bbox = bbox
        self.crs = crs
        self.terrain_size_m = terrain_size_m

        # Store bbox corners
        self.west = bbox["west"]
        self.south = bbox["south"]
        self.east = bbox["east"]
        self.north = bbox["north"]

        # Centre latitude for equirectangular fallback
        self.center_lat = (self.south + self.north) / 2.0

        # Try to set up pyproj transformer
        self._transformer_to_local = None
        self._transformer_to_wgs84 = None
        self._sw_projected = None
        self._ne_projected = None
        self._projected_width = None
        self._projected_depth = None
        self._use_pyproj = False

        self._setup_projection()
        self._compute_terrain_origin()

    def _compute_terrain_origin(self):
        """Place the terrain rectangle over the projected area.

        The terrain is ``terrain_size_m`` metres of the projected CRS, centred
        on the projected extent of what the user drew. Local coordinates are a
        pure translation from projected metres — **no per-axis scale** — so one
        metre on the ground is one metre in game on both axes, always.

        The pre-v1.18.0 code scaled each axis independently to force the
        projected extent into the terrain box (``target_w / projected_width``).
        That is an anisotropic squeeze: on the Hammarö map it was x0.97387 in X
        against x1.01954 in Z, a 4.69% difference, so a circle on the ground
        became a 4.7% ellipse in game and a 1 km measurement was ~25 m wrong
        one way and ~20 m the other (issue #203).

        Deriving the grid from the projected extent does **not** fix that on its
        own: each axis still snaps to a 128-face tile, which puts its scale back
        up to 128 m / extent away from 1.0, and the two axes round independently
        — worst case ~4.3% on a 6 km map, and it only measured 0.14% on Hammarö
        by luck. Removing the scale is what makes it structurally zero.

        Snapping is absorbed as *margin* instead: the terrain is a little larger
        than the drawn area (``snap_terrain_faces`` rounds up so it always
        contains it), centred, so the surplus is split between opposite edges.
        """
        if self.terrain_size_m is None:
            # No terrain box yet — local coords are offsets from the SW corner
            # of the projected extent.
            self._origin = (
                self._sw_projected if self._use_pyproj else (0.0, 0.0)
            )
            return

        target_w, target_d = self.terrain_size_m
        if self._use_pyproj:
            centre_x = (self._sw_projected[0] + self._ne_projected[0]) / 2.0
            centre_y = (self._sw_projected[1] + self._ne_projected[1]) / 2.0
        else:
            centre_x = self._projected_width / 2.0
            centre_y = self._projected_depth / 2.0

        self._origin = (centre_x - target_w / 2.0, centre_y - target_d / 2.0)

    def set_terrain_size(self, terrain_size_m: tuple[float, float]) -> None:
        """Attach the terrain box once its size is known.

        The grid size is derived from ``projected_width``/``projected_depth``,
        so the transformer has to exist before the terrain size does.
        """
        self.terrain_size_m = terrain_size_m
        self._compute_terrain_origin()

    @property
    def terrain_bounds_projected(self) -> tuple[float, float, float, float]:
        """The terrain rectangle as ``(min_x, min_y, max_x, max_y)`` in the CRS.

        This is *the* terrain footprint. Everything that has to line up in the
        finished map — the heightmap, the surface masks, the satellite warp and
        every spline — resolves against this one rectangle. Before #203 each of
        those derived its own footprint and they agreed only because everything
        was stretched to fit.
        """
        ox, oz = self._origin
        if self.terrain_size_m is None:
            return (ox, oz, ox + self._projected_width, oz + self._projected_depth)
        w, d = self.terrain_size_m
        return (ox, oz, ox + w, oz + d)

    def _setup_projection(self):
        """Set up pyproj transformers if available and CRS is not WGS84."""
        if self.crs in ("EPSG:4326", "", None):
            logger.info("Using equirectangular approximation (no metric CRS specified)")
            self._setup_equirectangular()
            return

        try:
            from pyproj import Transformer

            self._transformer_to_local = Transformer.from_crs(
                "EPSG:4326", self.crs, always_xy=True
            )
            self._transformer_to_wgs84 = Transformer.from_crs(
                self.crs, "EPSG:4326", always_xy=True
            )

            # Project the whole perimeter, not just two corners.
            #
            # A WGS84 lat/lon box is a *sheared quadrilateral* in a projected
            # CRS, not a rectangle. Taking NE-minus-SW as the extent (the
            # pre-v1.18.0 behaviour) therefore describes a box that two of the
            # four corners fall outside: on the Hammarö map the NW corner
            # landed at local z=6401 m in a terrain only 6277 m deep, so 247 m
            # of the area the user drew was never in the terrain at all
            # (issue #203). The edges bow as well as the corners, so sample
            # along them rather than transforming four points.
            lons, lats = self._bbox_perimeter_samples()
            xs, ys = self._transformer_to_local.transform(lons, lats)

            sw_x, sw_y = float(np.min(xs)), float(np.min(ys))
            ne_x, ne_y = float(np.max(xs)), float(np.max(ys))

            self._sw_projected = (sw_x, sw_y)
            self._ne_projected = (ne_x, ne_y)
            self._projected_width = ne_x - sw_x
            self._projected_depth = ne_y - sw_y

            self._use_pyproj = True

            logger.info(
                f"Coordinate transformer initialized: CRS={self.crs}, "
                f"projected extent: {self._projected_width:.1f}m x {self._projected_depth:.1f}m"
            )

        except ImportError:
            logger.warning("pyproj not available, falling back to equirectangular approximation")
            self._setup_equirectangular()
        except Exception as e:
            logger.warning(f"Failed to initialize pyproj with CRS {self.crs}: {e}")
            self._setup_equirectangular()

    def _bbox_perimeter_samples(self, samples_per_edge: int = 64):
        """Lon/lat arrays tracing the WGS84 bbox perimeter."""
        lon = np.linspace(self.west, self.east, samples_per_edge)
        lat = np.linspace(self.south, self.north, samples_per_edge)
        lons = np.concatenate([
            lon, lon,
            np.full(samples_per_edge, self.west),
            np.full(samples_per_edge, self.east),
        ])
        lats = np.concatenate([
            np.full(samples_per_edge, self.south),
            np.full(samples_per_edge, self.north),
            lat, lat,
        ])
        return lons, lats

    def _setup_equirectangular(self):
        """Set up equirectangular approximation for coordinate transformation."""
        self._use_pyproj = False

        # Metres per degree at the center latitude
        self._m_per_deg_lon = 111320.0 * math.cos(math.radians(self.center_lat))
        self._m_per_deg_lat = 110540.0

        self._projected_width = (self.east - self.west) * self._m_per_deg_lon
        self._projected_depth = (self.north - self.south) * self._m_per_deg_lat

        logger.info(
            f"Equirectangular approximation: "
            f"extent: {self._projected_width:.1f}m x {self._projected_depth:.1f}m "
            f"(center lat: {self.center_lat:.2f}°)"
        )

    def wgs84_envelope_of_projected_extent(
        self, samples_per_edge: int = 16
    ) -> tuple[float, float, float, float]:
        """
        Return the WGS84 bbox that fully encloses the projected terrain rectangle.

        The user picks a WGS84 lat/lon rectangle, but the terrain's pixel grid
        (heightmap, roads, satellite reprojection) is an axis-aligned rectangle
        in the projected CRS — and the two rectangles are *not* the same set of
        points. At high latitudes the meridians converge enough that the four
        corners of the projected rectangle, when unprojected, fall outside the
        original WGS84 bbox by up to ~1 km on a 5 km map.

        This method walks the perimeter of the projected rectangle, unprojects
        each sample to WGS84, and returns the min/max envelope. Use this as the
        WGS84 fetch bbox for satellite imagery so the reprojection step has
        source pixels covering every destination pixel.

        Returns:
            (west, south, east, north) in WGS84 degrees. For non-projected
            transformers this is the original bbox.
        """
        if not self._use_pyproj:
            return (self.west, self.south, self.east, self.north)

        sw_x, sw_y = self._sw_projected
        ne_x, ne_y = self._ne_projected

        xs = np.linspace(sw_x, ne_x, samples_per_edge)
        ys = np.linspace(sw_y, ne_y, samples_per_edge)
        edge_px = np.concatenate([
            xs, xs,
            np.full(samples_per_edge, sw_x), np.full(samples_per_edge, ne_x),
        ])
        edge_py = np.concatenate([
            np.full(samples_per_edge, sw_y), np.full(samples_per_edge, ne_y),
            ys, ys,
        ])

        lons, lats = self._transformer_to_wgs84.transform(edge_px, edge_py)
        return (
            float(np.min(lons)),
            float(np.min(lats)),
            float(np.max(lons)),
            float(np.max(lats)),
        )

    @property
    def projected_width(self) -> float:
        """Width of the projected terrain in metres."""
        return self._projected_width

    @property
    def projected_depth(self) -> float:
        """Depth (north-south extent) of the projected terrain in metres."""
        return self._projected_depth

    def wgs84_to_local(self, lon: float, lat: float) -> tuple[float, float]:
        """
        Convert WGS84 coordinates to Enfusion local metres.

        Args:
            lon: Longitude in degrees.
            lat: Latitude in degrees.

        Returns:
            (local_x, local_z) where x=East and z=North, in metres from origin.
        """
        if self._use_pyproj:
            px, py = self._transformer_to_local.transform(lon, lat)
        else:
            px = (lon - self.west) * self._m_per_deg_lon
            py = (lat - self.south) * self._m_per_deg_lat

        # Pure translation. No per-axis scale — see _compute_terrain_origin().
        return px - self._origin[0], py - self._origin[1]

    def local_to_wgs84(self, local_x: float, local_z: float) -> tuple[float, float]:
        """
        Convert Enfusion local metres back to WGS84 coordinates.

        Args:
            local_x: East distance from origin in metres.
            local_z: North distance from origin in metres.

        Returns:
            (longitude, latitude) in degrees.
        """
        px = local_x + self._origin[0]
        py = local_z + self._origin[1]

        if self._use_pyproj:
            lon, lat = self._transformer_to_wgs84.transform(px, py)
        else:
            lon = self.west + px / self._m_per_deg_lon
            lat = self.south + py / self._m_per_deg_lat

        return lon, lat

    def transform_geojson(self, geojson: dict) -> dict:
        """
        Transform an entire GeoJSON FeatureCollection to local Enfusion coordinates.

        Modifies coordinates in-place and returns the modified GeoJSON.
        Handles Point, LineString, MultiLineString, Polygon, MultiPolygon geometries.

        Args:
            geojson: GeoJSON FeatureCollection with WGS84 coordinates.

        Returns:
            Modified GeoJSON with local metre coordinates.
        """
        import copy
        result = copy.deepcopy(geojson)

        for feature in result.get("features", []):
            geom = feature.get("geometry", {})
            self._transform_geometry_coords(geom)

        return result

    def _transform_geometry_coords(self, geom: dict):
        """Recursively transform geometry coordinates from WGS84 to local."""
        geom_type = geom.get("type", "")

        if geom_type == "Point":
            coords = geom["coordinates"]
            x, z = self.wgs84_to_local(coords[0], coords[1])
            geom["coordinates"] = [x, z] + coords[2:]  # Preserve elevation if present

        elif geom_type == "LineString":
            geom["coordinates"] = [
                [*self.wgs84_to_local(c[0], c[1]), *(c[2:] if len(c) > 2 else [])]
                for c in geom["coordinates"]
            ]

        elif geom_type == "MultiLineString":
            geom["coordinates"] = [
                [
                    [*self.wgs84_to_local(c[0], c[1]), *(c[2:] if len(c) > 2 else [])]
                    for c in line
                ]
                for line in geom["coordinates"]
            ]

        elif geom_type == "Polygon":
            geom["coordinates"] = [
                [
                    [*self.wgs84_to_local(c[0], c[1]), *(c[2:] if len(c) > 2 else [])]
                    for c in ring
                ]
                for ring in geom["coordinates"]
            ]

        elif geom_type == "MultiPolygon":
            geom["coordinates"] = [
                [
                    [
                        [*self.wgs84_to_local(c[0], c[1]), *(c[2:] if len(c) > 2 else [])]
                        for c in ring
                    ]
                    for ring in polygon
                ]
                for polygon in geom["coordinates"]
            ]

        elif geom_type == "GeometryCollection":
            for sub_geom in geom.get("geometries", []):
                self._transform_geometry_coords(sub_geom)

    def transform_points(
        self,
        points: list[dict],
        elevation_array: Optional[np.ndarray] = None,
    ) -> list[dict]:
        """
        Transform a list of spline/road points from WGS84 to local coordinates.

        If ``elevation_array`` is provided, the Y (up) coordinate is sampled
        from the DEM so that spline points follow the terrain surface.
        Otherwise Y defaults to 0.

        Args:
            points: List of dicts with 'x' (lon) and 'y' (lat) keys.
            elevation_array: Optional DEM array (metres, north-up convention:
                             row 0 = north edge of bbox).

        Returns:
            List of dicts with 'x' (local_x), 'y' (elevation), 'z' (local_z).
        """
        # Pre-compute mapping constants for elevation sampling
        if elevation_array is not None:
            arr_h, arr_w = elevation_array.shape
            if self.terrain_size_m:
                tw, td = self.terrain_size_m
            else:
                tw = self._projected_width
                td = self._projected_depth

        result = []
        for p in points:
            local_x, local_z = self.wgs84_to_local(p["x"], p["y"])

            y_val = 0.0
            if elevation_array is not None:
                # Map local coords (0..tw, 0..td) to array pixel indices
                px = int(local_x / tw * (arr_w - 1))
                pz = int(local_z / td * (arr_h - 1))

                # Clamp to array bounds
                px = max(0, min(arr_w - 1, px))
                pz = max(0, min(arr_h - 1, pz))

                # Array is north-up: row 0 = north, local_z grows northward
                row = (arr_h - 1) - pz
                row = max(0, min(arr_h - 1, row))

                y_val = float(elevation_array[row, px])

            result.append({
                "x": round(local_x, 3),
                "y": round(y_val, 3),
                "z": round(local_z, 3),
            })
        return result

    def get_verification_data(self) -> dict:
        """
        Return verification data for the coordinate transformation.

        Useful for debugging and for the SETUP_GUIDE to show users that
        coordinates are correctly mapped.
        """
        # Transform all four corners
        sw_x, sw_z = self.wgs84_to_local(self.west, self.south)
        ne_x, ne_z = self.wgs84_to_local(self.east, self.north)
        nw_x, nw_z = self.wgs84_to_local(self.west, self.north)
        se_x, se_z = self.wgs84_to_local(self.east, self.south)

        center_lon = (self.west + self.east) / 2
        center_lat = (self.south + self.north) / 2
        center_x, center_z = self.wgs84_to_local(center_lon, center_lat)

        return {
            "method": "pyproj" if self._use_pyproj else "equirectangular",
            "crs": self.crs,
            "projected_width_m": self._projected_width,
            "projected_depth_m": self._projected_depth,
            "terrain_size_m": self.terrain_size_m,
            "corners": {
                "sw": {"wgs84": [self.west, self.south], "local": [sw_x, sw_z]},
                "ne": {"wgs84": [self.east, self.north], "local": [ne_x, ne_z]},
                "nw": {"wgs84": [self.west, self.north], "local": [nw_x, nw_z]},
                "se": {"wgs84": [self.east, self.south], "local": [se_x, se_z]},
            },
            "center": {
                "wgs84": [center_lon, center_lat],
                "local": [center_x, center_z],
            },
        }
