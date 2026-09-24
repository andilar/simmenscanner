"""Central configuration for SimsonRunner."""

from __future__ import annotations

import os

PROJECT_NAME = "SimsonRunner"
PROJECT_TITLE = "Simson Scanner"
BASE_URL = "https://www.kleinanzeigen.de"
HOME_POSTAL_CODE = "38533"
HOME_COORDINATES = (52.42, 10.60)
# Coarse centroids for the first two postal-code digits around the configured
# search area. Kleinanzeigen normally supplies a distance; these are the fallback.
POSTAL_PREFIX_COORDINATES = {
    "29": (52.63, 10.08), "30": (52.37, 9.74), "31": (52.15, 9.95),
    "32": (52.05, 8.70), "33": (51.95, 8.55), "34": (51.31, 9.49),
    "37": (51.53, 9.93), "38": (52.27, 10.52), "39": (52.13, 11.62),
    "06": (51.48, 11.97), "99": (50.98, 11.03),
}
SCAN_SCHEDULE = "07:00 Europe/Berlin"
MAX_PAGES = 20
REQUEST_DELAY_SECONDS = 1.0

SEARCH_URLS = {
    "S51": f"{BASE_URL}/s-motorraeder-roller/38533/sortierung:neuste/simson-s51/k0c305l2465r100",
    "KR51/1": f"{BASE_URL}/s-motorraeder-roller/38533/sortierung:neuste/simson-schwalbe-kr51-1/k0c305l2465r100",
    "KR51/2": f"{BASE_URL}/s-motorraeder-roller/38533/sortierung:neuste/simson-schwalbe-kr51-2/k0c305l2465r100",
}
ORIGINAL_SEARCH_URL = (
    f"{BASE_URL}/s-motorraeder-roller/38533/sortierung:neuste/"
    "simson-s51-kr51-schwalbe/k0c305l2465r100"
)
MARKET_DASHBOARD_URL = os.getenv(
    "MARKET_DASHBOARD_URL", "https://andilar.github.io/simmenscanner/"
)

# Price thresholds are intentionally conservative until enough market history exists.
# (excellent price, upper price receiving any bonus)
PRICE_RANGES = {"S51": (2_000, 5_000), "KR51/1": (1_800, 4_500), "KR51/2": (2_000, 5_000)}

EXCLUSION_TERMS = (
    "ersatzteil", "teilekonvolut", "nur teile", "motor einzeln", "rahmen einzeln",
    "modellauto", "modellfahrzeug", "literatur", "suche simson", "gesuch",
)
