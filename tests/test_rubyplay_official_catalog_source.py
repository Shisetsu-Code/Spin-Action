from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import requests

from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider


DOC_URL = "https://docs.rubyplay.com/content/integration/lists/game-list"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1AbC_def-123/edit?gid=987654321"
CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1AbC_def-123/gviz/tq?"
    "tqx=out%3Acsv&gid=987654321&range=A2%3AT&headers=1"
)
DOC_HTML = f'<html><body><a href="{SHEET_URL}">Game List Table</a></body></html>'
CSV_TEXT = '''RubyPlay Game List,,,,,,\nName,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\nUpcoming Game 96,Upcoming,2026-12-10,rp_999,5,Yes,https://demo.rubyplay.com/launcher?gamename=rp_999&mode=offline\nJ Mania Chili Champs 96,Active,2026-09-03,rp_214,5,Yes,https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline\nMad Hit Supernova 96,Active,2024-04-25,rp_108,10,Yes,https://rubyplay.com/games/mad-hit-supernova/\n'''


class _OfficialSession:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, *_args, **_kwargs):
        self.urls.append(url)
        if url == DOC_URL:
            body = DOC_HTML
        elif url == CSV_URL:
            body = CSV_TEXT
        else:
            raise AssertionError(f"unexpected network target: {url}")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = body.encode("utf-8")
        response.encoding = "utf-8"
        return response


class RubyPlayOfficialCatalogSourceTests(unittest.TestCase):
    def test_official_game_list_closes_catalog_without_website_pagination(self) -> None:
        messages: list[str] = []
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            session = _OfficialSession()
            provider.http = session  # type: ignore[assignment]
            games = provider.crawl_catalog(
                stop_event=threading.Event(),
                progress=messages.append,
                max_pages=0,
                on_game=lambda game: seen.append(game.slug),
            )

        self.assertEqual([game.symbol for game in games], ["rp_214", "rp_108"])
        self.assertEqual(seen, ["rp_214", "mad-hit-supernova"])
        self.assertEqual(session.urls, [DOC_URL, CSV_URL])
        self.assertTrue(provider.catalog_crawl_authoritative)
        self.assertIn("Game List", provider.catalog_crawl_reason)
        self.assertTrue(any("oficial" in message.casefold() for message in messages))
        self.assertEqual(provider.catalog_record_invalid_reason(games[0]), "")


if __name__ == "__main__":
    unittest.main()
