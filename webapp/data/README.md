# Bundled geodata

## `ne_10m_admin_0_countries.geojson`

- **Dataset:** Natural Earth — Admin 0 Countries, 1:10m scale
- **Source:** https://www.naturalearthdata.com/downloads/10m-cultural-vectors/
- **Mirror used for download:** https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson
- **Licence:** Public domain (no attribution required, but appreciated). See https://www.naturalearthdata.com/about/terms-of-use/
- **Used by:** `webapp/services/country_detector.py` — built into a Shapely `STRtree` at import time for offline country detection.

To refresh the file, re-download from the mirror URL above and overwrite this file.
Country features are matched on the `ISO_A2_EH` property (the "Egypt/Hala'ib"-corrected ISO A2 code).
