"""Country geographic data: CRS, names, treeline elevations.

Country detection uses the bundled Natural Earth 10m dataset
(``webapp/data/ne_10m_admin_0_countries.geojson``) via
``webapp/services/country_detector.py``; no per-country bounding-box
table is needed here.
"""

# CRS recommendations per country
COUNTRY_CRS: dict[str, str] = {
    "SE": "EPSG:3006",     # SWEREF99 TM
    "NO": "EPSG:25833",    # ETRS89 / UTM zone 33N
    "DK": "EPSG:25832",    # ETRS89 / UTM zone 32N
    "FI": "EPSG:3067",     # ETRS89 / TM35FIN
    "EE": "EPSG:3301",     # Estonian Coordinate System
    "LV": "EPSG:3059",     # LKS-92 / TM
    "LT": "EPSG:3346",     # LKS94 / Lithuania TM
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
