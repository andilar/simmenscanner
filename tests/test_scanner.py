from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scanner.market_history import dashboard_data, record_scan, render_dashboard
from scanner.scanner import (
    Listing, approximate_distance, atomic_json, build_email, detect_model, fetch_all, merge_favorites,
    parse_search_page, score_listing, validate_listings,
)


def card(ad_id="123", title="Simson S51", description="Originale DDR Papiere, 50 ccm original und VAPE. Fahrbereit.", price="2.500 € VB"):
    payload = json.dumps({"title": title, "description": description, "contentUrl": "https://img.example/1.jpg"})
    return f'''<li><article data-adid="{ad_id}" data-href="/s-anzeige/test/{ad_id}-305-1">
    <script type="application/ld+json">{payload}</script><span>38533 Vordorf</span><span class="ml-xsmall">(12 km)</span>
    <span>Heute, 08:00</span><h3><a>{title}</a></h3><p>{description}</p><p>{price}</p><span>EZ 05/1987</span><span>12.345 km</span></article></li>'''


def listing(ad_id="1", **changes):
    value = Listing(ad_id, "S51", "Simson S51", "2.500 € VB", "38533 Vordorf", "38533", f"https://example/{ad_id}", description="fahrbereit")
    for key, val in changes.items():
        setattr(value, key, val)
    return score_listing(value)


class ParserTests(unittest.TestCase):
    def test_current_markup_maps_listing(self):
        items, _ = parse_search_page(card())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.id, item.model, item.postal_code, item.distance_km), ("123", "S51", "38533", 12))
        self.assertEqual(item.model_year, "1987")
        self.assertIn("kleinanzeigen.de/s-anzeige", item.url)

    def test_next_page(self):
        source = card() + '<a title="Nächste" href="/seite:2" aria-label="Nächste">weiter</a>'
        _, next_url = parse_search_page(source, "https://www.kleinanzeigen.de/start")
        self.assertEqual(next_url, "https://www.kleinanzeigen.de/seite:2")

    def test_model_spellings_and_exclusions(self):
        for value, expected in [("S 51 Enduro", "S51"), ("KR 51/1", "KR51/1"), ("KR51-2", "KR51/2"), ("Schwalbe /1", "KR51/1")]:
            self.assertEqual(detect_model(value), expected)
        self.assertIsNone(detect_model("S51 Ersatzteil Rahmen einzeln"))
        self.assertIsNone(detect_model("Simson Star Moped S51 Optik"))

    def test_postal_prefix_distance_fallback(self):
        self.assertIsInstance(approximate_distance("38100"), int)
        self.assertIsNone(approximate_distance("00000"))

    @patch("scanner.scanner.fetch_url")
    def test_profiles_are_merged_and_deduplicated(self, mocked):
        mocked.return_value = card("same")
        result = fetch_all({"a": "https://x/a", "b": "https://x/b", "c": "https://x/c"}, max_pages=1)
        self.assertEqual([x.id for x in result], ["same"])

    def test_validation_rejects_empty_or_corrupt(self):
        with self.assertRaises(ValueError):
            validate_listings([])
        with self.assertRaises(ValueError):
            validate_listings([Listing("1", "S51", "", "", "", "", "")])


class ScoringTests(unittest.TestCase):
    def test_positive_scores(self):
        item = listing(description="Originale DDR Papiere. 60 km/h laut Betriebserlaubnis. Original 50 ccm, nummerngleich, 12V VAPE. Motor regeneriert, vollständig und fahrbereit.")
        self.assertEqual(item.score_breakdown["documents"], 100)
        self.assertGreaterEqual(item.score_breakdown["legal_specification"], 200)
        self.assertEqual(item.score_breakdown["technology"], 45)

    def test_bare_60_has_no_legal_bonus(self):
        item = listing(description="Läuft 60 km/h und ist fahrbereit")
        self.assertEqual(item.score_breakdown["legal_specification"], 0)
        self.assertTrue(any("Dokumentbezug" in risk for risk in item.risks))

    def test_risks_deduct(self):
        item = listing(description="Ohne Papiere, 70 ccm Tuningmotor, Rahmen geschweißt, nicht fahrbereit")
        self.assertEqual(item.score_breakdown["risk"], -355)

    def test_negation_wins(self):
        item = listing(description="VAPE nicht verbaut. Motor wird noch regeneriert. Keine Papiere.")
        self.assertEqual(item.score_breakdown["technology"], 0)
        self.assertEqual(item.score_breakdown["condition"], 0)
        self.assertEqual(item.score_breakdown["documents"], 0)


class OutputAndStateTests(unittest.TestCase):
    def test_email_order_and_escaping(self):
        items = [listing(str(i), title=f"<script>{i}</script>", score=20 - i) for i in range(5)]
        document = build_email(items, items[:3], len(items))
        self.assertNotIn("<script>0</script>", document)
        self.assertLess(document.index("Beste neue Angebote"), document.index("Weitere neue Angebote"))
        self.assertLess(document.index("Weitere neue Angebote"), document.index("All-Time-Favoriten"))
        self.assertIn("Heute" if any(x.online_since == "Heute" for x in items) else "Datum unbekannt", document)

    def test_favorites_keep_absent_listing(self):
        old = listing("old", description="Originale DDR Papiere, 50 ccm original, 12V VAPE, vollständig und fahrbereit")
        favorites = merge_favorites([old.as_dict()], [listing("new", description="fahrbereit")])
        self.assertEqual(favorites[0].id, "old")

    def test_seen_ids_are_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            atomic_json(path, sorted({"9", "2", "5"}))
            self.assertEqual(json.loads(path.read_text()), ["2", "5", "9"])


class HistoryTests(unittest.TestCase):
    def test_empty_dashboard_has_clear_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / "index.html"
            render_dashboard(root / "missing.sqlite", output)
            rendered = output.read_text()
            self.assertIn("Noch keine erfolgreichen Scans", rendered)

    def test_embedded_json_cannot_close_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = root / "history.sqlite"; output = root / "index.html"
            hostile = "</script><script>alert(1)</script>"
            record_scan(db, [listing("1", title=hostile, price="2.000 €")], "2026-09-23T07:00:00+00:00")
            record_scan(db, [listing("1", title=hostile, price="2.500 €")], "2026-09-24T07:00:00+00:00")
            render_dashboard(db, output)
            rendered = output.read_text()
            self.assertNotIn('"title":"</script>', rendered)
            self.assertIn("\\u003c/script>", rendered)

    def test_consecutive_snapshots_and_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = root / "history.sqlite"; output = root / "index.html"
            first = [listing("1", price="2.000 €"), listing("2", price="4.000 €", model="KR51/1")]
            second = [listing("1", price="3.000 €"), listing("3", price="5.000 €", model="KR51/2")]
            record_scan(db, first, "2026-09-23T07:00:00+00:00")
            record_scan(db, second, "2026-09-24T07:00:00+00:00")
            data = dashboard_data(db)
            self.assertEqual(data["last_scan"]["median_price"], 4000)
            self.assertEqual((data["last_scan"]["new_count"], data["last_scan"]["removed_count"]), (1, 1))
            self.assertEqual(data["last_scan"]["increased_count"], 1)
            self.assertEqual(len(data["price_changes"]), 1)
            render_dashboard(db, output)
            rendered = output.read_text()
            self.assertIn("Simson Marktübersicht", rendered)
            self.assertIn("2026-09-24", rendered)
            self.assertNotIn("__DASHBOARD_DATA__", rendered)


if __name__ == "__main__":
    unittest.main()
