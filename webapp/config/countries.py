"""Country geographic data: bounding boxes, CRS, names, treeline elevations."""

# ---------------------------------------------------------------------------
# Country bounding boxes (approx WGS84) — (lat_south, lon_west, lat_north, lon_east)
# ---------------------------------------------------------------------------
COUNTRY_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    # Nordics + Baltics
    "SE": (55.34, 10.96, 69.06, 24.17),
    "NO": (57.97,  4.64, 71.19, 31.17),
    "DK": (54.56,  8.07, 57.75, 15.20),
    "FI": (59.81, 20.55, 70.09, 31.59),
    "EE": (57.52, 21.76, 59.68, 28.21),
    "LV": (55.67, 20.97, 58.09, 28.24),
    "LT": (53.90, 20.93, 56.45, 26.84),
    # Extended
    "DE": (47.30,  5.90, 55.10, 15.00),
    "PL": (49.00, 14.10, 54.80, 24.20),
    "RU": (41.20, 19.60, 81.90, 180.00),
    "GB": (49.90, -8.20, 60.90,  1.80),
    "FR": (42.30, -5.10, 51.10,  8.20),
    "ES": (36.00, -9.30, 43.80,  3.30),
    "IT": (36.60,  6.60, 47.10, 18.50),
    "AT": (46.40,  9.50, 49.00, 17.20),
    "CH": (45.80,  5.90, 47.80, 10.50),
    "CZ": (48.50, 12.10, 51.10, 18.90),
    "NL": (50.80,  3.40, 53.50,  7.20),
    "BE": (49.50,  2.50, 51.50,  6.40),
    "UA": (44.40, 22.10, 52.40, 40.20),
    "RO": (43.60, 20.30, 48.30, 29.70),
    "HU": (45.70, 16.10, 48.60, 22.90),
    "SK": (47.70, 16.80, 49.60, 22.60),
    "HR": (42.40, 13.50, 46.50, 19.40),
    "RS": (42.20, 18.80, 46.20, 23.00),
    "BG": (41.20, 22.40, 44.20, 28.60),
    "GR": (34.80, 19.40, 41.70, 29.60),
    "PT": (37.00, -9.50, 42.20, -6.20),
    "IE": (51.40, -10.50, 55.40, -6.00),
    "IS": (63.30, -24.50, 66.50, -13.50),
}

# CRS recommendations per country
COUNTRY_CRS: dict[str, str] = {
    "SE": "EPSG:3006",     # SWEREF99 TM
    "NO": "EPSG:25833",    # ETRS89 / UTM zone 33N
    "DK": "EPSG:25832",    # ETRS89 / UTM zone 32N
    "FI": "EPSG:3067",     # ETRS89 / TM35FIN
    "EE": "EPSG:3301",     # Estonian Coordinate System
    "LV": "EPSG:3059",     # LKS-92 / TM
    "LT": "EPSG:4258",     # ETRS89 geographic
    "DE": "EPSG:25832",
    "PL": "EPSG:2180",
    "GB": "EPSG:27700",
    "FR": "EPSG:2154",
    "ES": "EPSG:25830",
    "IT": "EPSG:32632",
    "AT": "EPSG:31287",
    "CH": "EPSG:2056",
}

# Human-readable country names
COUNTRY_NAMES: dict[str, str] = {
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "DE": "Germany", "PL": "Poland", "RU": "Russia", "GB": "United Kingdom",
    "FR": "France", "ES": "Spain", "IT": "Italy", "AT": "Austria",
    "CH": "Switzerland", "CZ": "Czech Republic", "NL": "Netherlands",
    "BE": "Belgium", "UA": "Ukraine", "RO": "Romania", "HU": "Hungary",
    "SK": "Slovakia", "HR": "Croatia", "RS": "Serbia", "BG": "Bulgaria",
    "GR": "Greece", "PT": "Portugal", "IE": "Ireland", "IS": "Iceland",
}

# Treeline elevations per country (metres above sea level)
TREELINE_ELEVATION: dict[str, int] = {
    "NO": 1100,
    "SE": 1000,
    "FI":  600,
    "DK": 9999,
    "EE": 9999,
    "LV": 9999,
    "LT": 9999,
    "PL": 1400,  # Tatra Mountains treeline
}
