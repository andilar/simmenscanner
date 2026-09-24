#!/usr/bin/env python3
"""Fetch, classify and report Kleinanzeigen listings for selected Simson models."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import logging
import math
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scanner import config
else:
    from . import config

LOG = logging.getLogger("simsonrunner")
ROOT = Path(__file__).resolve().parents[1]
SCANNER_DIR = ROOT / "scanner"


@dataclasses.dataclass
class Listing:
    id: str
    model: str | None
    title: str
    price: str
    location: str
    postal_code: str
    url: str
    online_since: str = ""
    mileage: str = ""
    model_year: str | int = ""
    image_url: str = ""
    description: str = ""
    distance_km: int | None = None
    score: int = 0
    score_breakdown: dict[str, int] = dataclasses.field(default_factory=dict)
    positives: list[str] = dataclasses.field(default_factory=list)
    risks: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Listing":
        allowed = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: val for key, val in value.items() if key in allowed})


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def detect_model(text: str, description: str = "") -> str | None:
    title = text.casefold()
    value = f"{text} {description}".casefold()
    if any(term in value for term in config.EXCLUSION_TERMS):
        return None
    patterns = (
        (r"\b(?:kr\s*51\s*[/\-]?\s*1|schwalbe\s*(?:/\s*1|1))\b", "KR51/1"),
        (r"\b(?:kr\s*51\s*[/\-]?\s*2|schwalbe\s*(?:/\s*2|2))\b", "KR51/2"),
        (r"\bs\s*51(?:\b|[-/])", "S51"),
    )
    if re.search(r"\b(?:star|sperber|habicht|s\s*50|sr\s*50|kr\s*50|spatz)\b", title):
        return None
    for pattern, model in patterns:
        if re.search(pattern, title):
            return model
    if re.search(patterns[0][0], value):
        return "KR51/1"
    if re.search(patterns[1][0], value):
        return "KR51/2"
    if re.search(patterns[2][0], value):
        return "S51"
    return None


def parse_price(value: str) -> int | None:
    match = re.search(r"(\d[\d.]*)\s*€", value)
    return int(match.group(1).replace(".", "")) if match else None


def approximate_distance(postal_code: str) -> int | None:
    target = config.POSTAL_PREFIX_COORDINATES.get(postal_code[:2])
    if not target:
        return None
    lat1, lon1 = map(math.radians, config.HOME_COORDINATES)
    lat2, lon2 = map(math.radians, target)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(value)))


def _extract_ld(fragment: str) -> dict:
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        fragment,
        flags=re.I | re.S,
    ):
        try:
            value = json.loads(html.unescape(raw))
            if isinstance(value, dict) and (value.get("title") or value.get("name")):
                return value
        except json.JSONDecodeError:
            continue
    return {}


def parse_search_page(source: str, page_url: str = config.BASE_URL) -> tuple[list[Listing], str | None]:
    listings: list[Listing] = []
    fragments = re.findall(
        r"<article\b(?P<attrs>[^>]*\bdata-adid=[\"'][^\"']+[\"'][^>]*)>(?P<body>.*?)</article>",
        source,
        flags=re.I | re.S,
    )
    for attrs, body in fragments:
        id_match = re.search(r"data-adid=[\"']([^\"']+)", attrs, re.I)
        href_match = re.search(r"data-href=[\"']([^\"']+)", attrs, re.I)
        if not id_match:
            continue
        ld = _extract_ld(body)
        title = clean_text(str(ld.get("title") or ld.get("name") or ""))
        if not title:
            title_match = re.search(r"<h3\b[^>]*>.*?<a\b[^>]*>(.*?)</a>", body, re.I | re.S)
            title = clean_text(title_match.group(1)) if title_match else ""
        description = clean_text(str(ld.get("description") or ""))
        combined = f"{title} {description}"
        model = detect_model(title, description)
        if model is None:
            continue
        visible = clean_text(body)
        price_match = re.search(r"(?:^|\s)(\d[\d.]*\s*€(?:\s*VB)?)", visible, re.I)
        loc_match = re.search(r"\b(\d{5})\s+([^()|]{2,60}?)(?:\s*\((\d+)\s*km\))?(?:\s|$)", visible)
        date_match = re.search(r"\b(Heute|Gestern|\d{2}\.\d{2}\.\d{4})(?:,\s*\d{2}:\d{2})?", visible, re.I)
        mileage_matches = re.findall(r'<span(?![^>]*ml-xsmall)[^>]*>\s*([\d.]+\s*km)\s*</span>', body, re.I)
        year_match = re.search(r"(?:EZ|Baujahr|Erstzulassung)\s*(?:\d{1,2}/)?(19\d{2}|20\d{2})", combined + " " + visible, re.I)
        image_url = str(ld.get("contentUrl") or ld.get("image") or "")
        if not image_url:
            img_match = re.search(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", body, re.I)
            image_url = html.unescape(img_match.group(1)) if img_match else ""
        href = href_match.group(1) if href_match else ""
        if not href:
            href_from_body = re.search(r"href=[\"']([^\"']*/s-anzeige/[^\"']+)", body, re.I)
            href = href_from_body.group(1) if href_from_body else ""
        postal_code = loc_match.group(1) if loc_match else ""
        supplied_distance = int(loc_match.group(3)) if loc_match and loc_match.group(3) else None
        listings.append(Listing(
            id=id_match.group(1), model=model, title=title,
            price=price_match.group(1) if price_match else "",
            location=(f"{loc_match.group(1)} {loc_match.group(2).strip()}" if loc_match else ""),
            postal_code=postal_code,
            url=urljoin(config.BASE_URL, html.unescape(href)),
            online_since=date_match.group(1) if date_match else "",
            mileage=clean_text(mileage_matches[-1]) if mileage_matches else "",
            model_year=year_match.group(1) if year_match else "",
            image_url=image_url, description=description,
            distance_km=supplied_distance if supplied_distance is not None else approximate_distance(postal_code),
        ))
    next_match = re.search(
        r'<a\b[^>]*(?:title|aria-label)=["\']Nächste["\'][^>]*href=["\']([^"\']+)["\']|'
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*(?:title|aria-label)=["\']Nächste["\']',
        source,
        flags=re.I,
    )
    next_url = urljoin(page_url, next(filter(None, next_match.groups()))) if next_match else None
    return listings, next_url


def fetch_url(url: str, attempts: int = 3) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"Abruf nach {attempts} Versuchen fehlgeschlagen: {url}: {last_error}")


def fetch_all(search_urls: dict[str, str] | None = None, max_pages: int | None = None) -> list[Listing]:
    max_pages = max_pages or config.MAX_PAGES
    found: dict[str, Listing] = {}
    visited: set[str] = set()
    for profile, start_url in (search_urls or config.SEARCH_URLS).items():
        url: str | None = start_url
        pages = 0
        while url and url not in visited and pages < max_pages:
            LOG.info("Rufe %s auf: %s", profile, url)
            visited.add(url)
            source = fetch_url(url)
            if os.getenv("DEBUG") == "1":
                (SCANNER_DIR / f"debug_{profile.replace('/', '_')}_{pages + 1}.html").write_text(source, encoding="utf-8")
            page_listings, url = parse_search_page(source, url)
            for listing in page_listings:
                found[listing.id] = listing
            pages += 1
            if url and pages < max_pages:
                time.sleep(config.REQUEST_DELAY_SECONDS)
    return list(found.values())


def validate_listings(listings: list[Listing]) -> None:
    if not listings:
        raise ValueError("Keine Ergebnisse erkannt; Zustand wurde nicht aktualisiert")
    complete = sum(bool(item.title and not item.title.isnumeric() and item.price and item.url) for item in listings)
    if complete / len(listings) < 0.5:
        raise ValueError(
            f"Parser-Feldabdeckung zu niedrig ({complete}/{len(listings)} vollständig); Zustand wurde nicht aktualisiert"
        )


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.I) is not None


def score_listing(listing: Listing) -> Listing:
    text = f"{listing.title} {listing.description}".casefold()
    breakdown = {key: 0 for key in ("documents", "legal_specification", "technology", "condition", "price", "distance", "risk")}
    positives: list[str] = []
    risks: list[str] = []
    missing_docs = _has(text, r"ohne\s+(?:ddr[ -]?)?papiere|keine\s+papiere|papiere\s+(?:müssen|werden)\s+(?:noch\s+)?beantragt")
    if missing_docs:
        breakdown["risk"] -= 120; risks.append("Papiere fehlen/zu beantragen")
    elif _has(text, r"ddr[ -]?(?:betriebserlaubnis|papiere)|originalpapiere|originale?\s+ddr\s+papiere"):
        breakdown["documents"] = 100; positives.append("DDR-Originalpapiere (laut Anbieter)")
    elif _has(text, r"kba[ -]?(?:papiere|nachweis|betriebserlaubnis)"):
        breakdown["documents"] = 90; positives.append("KBA-Nachweis (laut Anbieter)")
    documented_60 = _has(text, r"60\s*km/?h") and _has(text, r"betriebserlaubnis|\babe\b|gutachten|papiere")
    if documented_60:
        breakdown["legal_specification"] += 80; positives.append("60 km/h dokumentiert (vor Kauf prüfen)")
    elif _has(text, r"60\s*km/?h"):
        risks.append("60 km/h ohne erkennbaren Dokumentbezug")
    tuned = _has(text, r"\b(?:60|70|75|85)\s*(?:ccm|cm³)|sportzylinder|tuningmotor")
    if tuned:
        breakdown["risk"] -= 120; risks.append("Hubraum/Tuning über 50 cm³")
    elif _has(text, r"50\s*(?:ccm|cm³)") and _has(text, r"original|ungetunt|kein\s+tuning"):
        breakdown["legal_specification"] += 70; positives.append("Originaler 50-cm³-Zustand (laut Anbieter)")
    if _has(text, r"nummerngleich|fahrgestellnummer.{0,50}(?:stimmt|passend|überein)"):
        breakdown["legal_specification"] += 50; positives.append("Fahrgestellnummer passend (laut Anbieter)")
    if not _has(text, r"vape\s+(?:nicht|noch nicht)|keine\s+vape") and _has(text, r"\bvape\b|12\s*v\s*vape"):
        breakdown["technology"] += 45; positives.append("VAPE-Zündung")
    if not _has(text, r"motor\s+wird\s+(?:noch\s+)?(?:regeneriert|überholt)") and _has(text, r"motor\s+(?:regeneriert|überholt)"):
        breakdown["condition"] += 30; positives.append("Motor regeneriert/überholt")
    if _has(text, r"originalzustand|vollständig") and not tuned:
        breakdown["condition"] += 20; positives.append("Originalzustand/vollständig")
    if _has(text, r"fahrbereit|fährt\s+(?:gut|einwandfrei)|guter\s+technischer\s+zustand") and not _has(text, r"nicht\s+fahrbereit"):
        breakdown["condition"] += 15; positives.append("Fahrbereit (laut Anbieter)")
    if _has(text, r"reimport|ungarn|export") and not _has(text, r"einzelbetriebserlaubnis|gutachten"):
        breakdown["risk"] -= 60; risks.append("Reimport/Export – Zulässigkeit prüfen")
    if _has(text, r"defekt|bastler|nicht\s+fahrbereit"):
        breakdown["risk"] -= 35; risks.append("Defekt/Bastler/nicht fahrbereit")
    if _has(text, r"rahmenriss|rahmen\s+(?:ist\s+)?geschwei(?:ß|ss)t"):
        breakdown["risk"] -= 80; risks.append("Rahmenschaden/geschweißter Rahmen")
    numeric_price = parse_price(listing.price)
    if numeric_price and listing.model in config.PRICE_RANGES:
        low, high = config.PRICE_RANGES[listing.model]
        if numeric_price < low * 0.45:
            risks.append("Unrealistisch niedriger Preis – prüfen")
        elif numeric_price <= low:
            breakdown["price"] = 40
        elif numeric_price < high:
            breakdown["price"] = round(40 * (high - numeric_price) / (high - low))
    if listing.distance_km is not None:
        if listing.distance_km <= 50:
            breakdown["distance"] = 20
        elif listing.distance_km < 600:
            breakdown["distance"] = round(20 * (600 - listing.distance_km) / 550)
    listing.score_breakdown = breakdown
    listing.score = sum(breakdown.values())
    listing.positives = positives
    listing.risks = risks
    return listing


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def merge_favorites(previous: list[dict], current: list[Listing]) -> list[Listing]:
    merged = {item.id: score_listing(item) for item in (Listing.from_dict(raw) for raw in previous)}
    merged.update({item.id: item for item in current})
    return sorted(merged.values(), key=lambda item: (-item.score, item.id))[:3]


def _badges(values: list[str], css_class: str) -> str:
    return "".join(f'<span class="badge {css_class}">{html.escape(value)}</span>' for value in values)


def _card(item: Listing, rank: int) -> str:
    image = f'<img src="{html.escape(item.image_url, quote=True)}" alt="">' if item.image_url else '<div class="placeholder">Simme</div>'
    desc = item.description[:217] + "…" if len(item.description) > 220 else item.description
    return f'''<article class="card">{image}<div><div class="rank">#{rank} · {item.score} Punkte · {html.escape(item.model or "Unklar")}</div>
    <h3><a href="{html.escape(item.url, quote=True)}">{html.escape(item.title)}</a></h3>
    <p class="facts"><strong>{html.escape(item.price or "Preis fehlt")}</strong> · Bj. {html.escape(str(item.model_year or "–"))} · {html.escape(item.mileage or "km unbekannt")}<br>{html.escape(item.location or "Ort unbekannt")} · {item.distance_km if item.distance_km is not None else "–"} km · {html.escape(item.online_since or "Datum unbekannt")}</p>
    <div>{_badges(item.positives, "good")}{_badges(item.risks, "risk")}</div><p>{html.escape(desc)}</p></div></article>'''


def build_email(new: list[Listing], favorites: list[Listing], total: int, scan_time: dt.datetime | None = None) -> str:
    scan_time = scan_time or dt.datetime.now(dt.timezone.utc)
    top = sorted(new, key=lambda item: (-item.score, item.id))[:3]
    rest = sorted(new, key=lambda item: (-item.score, item.id))[3:]
    rows = "".join(
        f'<tr><td><a href="{html.escape(x.url, quote=True)}">{html.escape(x.title)}</a><br><small>{html.escape(x.model or "")}</small></td><td>{html.escape(x.price or "–")}</td><td>{html.escape(str(x.model_year or "–"))}</td><td>{html.escape(x.mileage or "–")}</td><td>{html.escape(x.location or "–")} ({x.distance_km if x.distance_km is not None else "–"} km)</td></tr>'
        for x in rest
    ) or '<tr><td colspan="5">Keine weiteren neuen Angebote.</td></tr>'
    top_html = "".join(_card(item, rank) for rank, item in enumerate(top, 1)) or "<p><strong>Keine neuen Angebote.</strong></p>"
    favorite_html = "".join(_card(item, rank) for rank, item in enumerate(favorites, 1)) or "<p>Noch keine Favoriten vorhanden.</p>"
    highlighted = sum(bool(item.positives) for item in new)
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><style>
    body{{font:16px Arial,sans-serif;color:#25231f;background:#f3efe5;margin:0}}main{{max-width:900px;margin:auto;padding:24px}}header{{background:#722f20;color:white;padding:24px;border-radius:14px}}a{{color:#7b2d20}}.stats{{display:flex;gap:12px;margin:18px 0;flex-wrap:wrap}}.stat{{background:white;padding:12px 18px;border-radius:10px}}.card{{display:grid;grid-template-columns:180px 1fr;gap:18px;background:white;padding:16px;margin:12px 0;border-radius:12px;border-left:6px solid #d18b28}}.card img,.placeholder{{width:180px;height:135px;object-fit:cover;border-radius:8px;background:#ddd;display:flex;align-items:center;justify-content:center}}.rank{{color:#722f20;font-weight:bold}}.facts{{line-height:1.5}}.badge{{display:inline-block;padding:5px 8px;margin:2px;border-radius:12px;font-size:12px}}.good{{background:#d8edcf}}.risk{{background:#ffd9d2}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}@media(max-width:600px){{.card{{grid-template-columns:1fr}}.card img,.placeholder{{width:100%}}}}
    </style></head><body><main><header><h1>{config.PROJECT_TITLE}</h1><p>Marktbericht vom {scan_time:%d.%m.%Y}</p></header>
    <div class="stats"><div class="stat"><strong>{len(new)}</strong><br>neu</div><div class="stat"><strong>{highlighted}</strong><br>mit Highlights</div><div class="stat"><strong>{total}</strong><br>aktuell</div></div>
    <h2>Beste neue Angebote</h2>{top_html}<h2>Weitere neue Angebote</h2><table><thead><tr><th>Angebot</th><th>Preis</th><th>Baujahr</th><th>km</th><th>Ort</th></tr></thead><tbody>{rows}</tbody></table>
    <h2>All-Time-Favoriten</h2>{favorite_html}<p><a href="{html.escape(config.ORIGINAL_SEARCH_URL, quote=True)}">Originalsuche</a> · <a href="{html.escape(config.MARKET_DASHBOARD_URL, quote=True)}">Markt-Dashboard</a></p>
    <p><small>Automatische Bewertung von Anbieterangaben. Papiere, Identität, Zustand und Zulässigkeit vor dem Kauf selbst prüfen.</small></p></main></body></html>'''


def send_email(document: str, new_count: int) -> None:
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("NOTIFY_EMAIL") or user
    if not user or not password or not recipient:
        raise RuntimeError("SMTP-Konfiguration unvollständig")
    message = EmailMessage()
    label = f"{new_count} neue Angebote" if new_count else "Keine neuen Angebote"
    message["Subject"] = f"{config.PROJECT_NAME}: {label} – {dt.date.today():%d.%m.%Y}"
    message["From"], message["To"] = user, recipient
    message.set_content("Dieser Bericht benötigt eine HTML-fähige E-Mail-Anzeige.")
    message.add_alternative(document, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)


def run() -> int:
    LOG.info("Scan gestartet")
    listings = [score_listing(item) for item in fetch_all()]
    validate_listings(listings)
    seen_path = SCANNER_DIR / "last_seen_ids.json"
    favorites_path = SCANNER_DIR / "all_time_favorites.json"
    seen = set(load_json(seen_path, []))
    new = [item for item in listings if item.id not in seen]
    favorites = merge_favorites(load_json(favorites_path, []), listings)
    LOG.info("%d Angebote erkannt, %d neu", len(listings), len(new))
    document = build_email(new, favorites, len(listings))
    if not os.getenv("GMAIL_USER") or not os.getenv("GMAIL_APP_PASSWORD"):
        (SCANNER_DIR / "preview.html").write_text(document, encoding="utf-8")
        LOG.info("Keine SMTP-Secrets: nur scanner/preview.html erzeugt; State unverändert")
        return 0
    send_email(document, len(new))
    atomic_json(seen_path, sorted(seen | {item.id for item in listings}))
    atomic_json(favorites_path, [item.as_dict() for item in favorites])
    from scanner.market_history import record_scan, render_dashboard
    record_scan(SCANNER_DIR / "market_history.sqlite", listings)
    render_dashboard(SCANNER_DIR / "market_history.sqlite", ROOT / "docs" / "index.html")
    LOG.info("E-Mail versendet und Zustand aktualisiert")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SimsonRunner Markt-Scanner")
    parser.add_argument("--max-pages", type=int, help="Temporäres Seitenlimit")
    args = parser.parse_args()
    if args.max_pages:
        config.MAX_PAGES = args.max_pages
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run()
    except Exception:
        LOG.exception("Scan fehlgeschlagen; persistenter Zustand wurde nicht aktualisiert")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
