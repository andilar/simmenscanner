"""SQLite market history and static dashboard generation."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from statistics import mean, median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scanner import Listing


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL UNIQUE,
    total_count INTEGER NOT NULL,
    priced_count INTEGER NOT NULL,
    median_price REAL,
    average_price REAL,
    min_price INTEGER,
    max_price INTEGER,
    new_count INTEGER NOT NULL,
    removed_count INTEGER NOT NULL,
    increased_count INTEGER NOT NULL,
    decreased_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    listing_id TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    model TEXT,
    price INTEGER,
    price_text TEXT,
    location TEXT,
    postal_code TEXT,
    mileage TEXT,
    model_year TEXT,
    score INTEGER NOT NULL,
    positives TEXT NOT NULL,
    risks TEXT NOT NULL,
    PRIMARY KEY (scan_id, listing_id)
);
CREATE INDEX IF NOT EXISTS observations_listing_idx ON observations(listing_id, scan_id);
CREATE INDEX IF NOT EXISTS observations_scan_idx ON observations(scan_id);
"""


def _price(value: str) -> int | None:
    from .scanner import parse_price
    return parse_price(value)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def record_scan(path: Path, listings: list["Listing"], scanned_at: str | None = None) -> int:
    timestamp = scanned_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    current = {item.id: item for item in listings}
    prices = [_price(item.price) for item in listings]
    numeric_prices = [value for value in prices if value is not None]
    with closing(connect(path)) as db, db:
        previous_scan = db.execute("SELECT id FROM scans ORDER BY scanned_at DESC LIMIT 1").fetchone()
        previous: dict[str, int | None] = {}
        if previous_scan:
            previous = {
                row["listing_id"]: row["price"]
                for row in db.execute("SELECT listing_id, price FROM observations WHERE scan_id = ?", (previous_scan["id"],))
            }
        new_count = len(set(current) - set(previous)) if previous_scan else len(current)
        removed_count = len(set(previous) - set(current))
        increased = sum(
            previous[item_id] is not None and _price(item.price) is not None and _price(item.price) > previous[item_id]
            for item_id, item in current.items() if item_id in previous
        )
        decreased = sum(
            previous[item_id] is not None and _price(item.price) is not None and _price(item.price) < previous[item_id]
            for item_id, item in current.items() if item_id in previous
        )
        cursor = db.execute(
            """INSERT INTO scans(scanned_at,total_count,priced_count,median_price,average_price,min_price,max_price,
               new_count,removed_count,increased_count,decreased_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                timestamp, len(listings), len(numeric_prices), median(numeric_prices) if numeric_prices else None,
                mean(numeric_prices) if numeric_prices else None, min(numeric_prices) if numeric_prices else None,
                max(numeric_prices) if numeric_prices else None, new_count, removed_count, increased, decreased,
            ),
        )
        scan_id = cursor.lastrowid
        for item in listings:
            db.execute(
                """INSERT INTO listings(id,title,url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,last_seen_at=excluded.last_seen_at""",
                (item.id, item.title, item.url, timestamp, timestamp),
            )
            db.execute(
                """INSERT INTO observations(scan_id,listing_id,model,price,price_text,location,postal_code,mileage,
                   model_year,score,positives,risks) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id, item.id, item.model, _price(item.price), item.price, item.location, item.postal_code,
                    item.mileage, str(item.model_year), item.score, json.dumps(item.positives, ensure_ascii=False),
                    json.dumps(item.risks, ensure_ascii=False),
                ),
            )
        return int(scan_id)


def dashboard_data(path: Path) -> dict:
    if not path.exists():
        return {"last_scan": None, "series": [], "models": [], "price_changes": []}
    with closing(connect(path)) as db:
        scans = [dict(row) for row in db.execute("SELECT * FROM scans ORDER BY scanned_at DESC LIMIT 90")]
        scans.reverse()
        if not scans:
            return {"last_scan": None, "series": [], "models": [], "price_changes": []}
        latest_id = scans[-1]["id"]
        models = []
        for model in ("S51", "KR51/1", "KR51/2"):
            row = db.execute(
                "SELECT COUNT(*) count, COUNT(price) priced, MIN(price) minimum, MAX(price) maximum FROM observations WHERE scan_id=? AND model=?",
                (latest_id, model),
            ).fetchone()
            values = [r[0] for r in db.execute(
                "SELECT price FROM observations WHERE scan_id=? AND model=? AND price IS NOT NULL ORDER BY price",
                (latest_id, model),
            )]
            models.append({"model": model, **dict(row), "median": median(values) if values else None})
        changes = [dict(row) for row in db.execute(
            """WITH ordered AS (
                 SELECT o.listing_id,o.scan_id,o.price,s.scanned_at,l.title,l.url,
                        LAG(o.price) OVER(PARTITION BY o.listing_id ORDER BY s.scanned_at) previous_price
                 FROM observations o JOIN scans s ON s.id=o.scan_id JOIN listings l ON l.id=o.listing_id
               ) SELECT scanned_at,title,url,previous_price,price current_price FROM ordered
               WHERE previous_price IS NOT NULL AND price IS NOT NULL AND price != previous_price
               ORDER BY scanned_at DESC LIMIT 50"""
        )]
        last_scan = scans[-1]
        last_scan["median_change"] = (
            last_scan["median_price"] - scans[-2]["median_price"]
            if len(scans) > 1 and last_scan["median_price"] is not None and scans[-2]["median_price"] is not None else None
        )
        return {"last_scan": last_scan, "series": scans, "models": models, "price_changes": changes}


def render_dashboard(path: Path, output: Path) -> None:
    template = (Path(__file__).with_name("dashboard_template.html")).read_text(encoding="utf-8")
    payload = json.dumps(dashboard_data(path), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".html.tmp")
    temporary.write_text(template.replace("__DASHBOARD_DATA__", payload), encoding="utf-8")
    temporary.replace(output)
