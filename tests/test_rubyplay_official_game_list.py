from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.official_game_list import (
    build_sheet_csv_url,
    discover_game_list_sheet_url,
    parse_official_game_list_csv,
)


DOC_HTML = r'''
<html><body>
<a href="https://docs.google.com/spreadsheets/d/1AbC_def-123/edit?gid=987654321">Game List Table</a>
</body></html>
'''

CSV_TEXT = '''RubyPlay Game List,,,,,,\nName,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\nUpcoming Game 96,Upcoming,2026-12-10,rp_999,5,Yes,https://demo.rubyplay.com/launcher?gamename=rp_999&mode=offline\nJ Mania Chili Champs 96,Active,2026-09-03,rp_214,5,Yes,https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline\nMad Hit Supernova 96,Active,2024-04-25,rp_108,10,Yes,https://rubyplay.com/games/mad-hit-supernova/\n'''


class RubyPlayOfficialGameListTests(unittest.TestCase):
    def test_discovers_google_sheet_and_builds_csv_export_url(self) -> None:
        sheet = discover_game_list_sheet_url(DOC_HTML, "https://docs.rubyplay.com/content/integration/lists/game-list")
        self.assertEqual(
            sheet,
            "https://docs.google.com/spreadsheets/d/1AbC_def-123/edit?gid=987654321",
        )
        self.assertEqual(
            build_sheet_csv_url(sheet),
            "https://docs.google.com/spreadsheets/d/1AbC_def-123/gviz/tq?tqx=out%3Acsv&gid=987654321",
        )

    def test_parser_keeps_only_active_games_and_preserves_provider_identity(self) -> None:
        records = parse_official_game_list_csv(CSV_TEXT, provider_key="rubyplay")
        self.assertEqual(len(records), 2)

        direct = next(item for item in records if item.game.symbol == "rp_214")
        self.assertEqual(direct.game.slug, "rp_214")
        self.assertEqual(direct.game.name, "J Mania Chili Champs 96")
        self.assertEqual(
            direct.game.url,
            "https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline",
        )
        self.assertEqual(direct.status, "Active")
        self.assertEqual(direct.release_date, "2026-09-03")
        self.assertEqual(direct.wager, "5")
        self.assertTrue(direct.buy_feature)

        public = next(item for item in records if item.game.symbol == "rp_108")
        self.assertEqual(public.game.slug, "mad-hit-supernova")
        self.assertEqual(public.game.url, "https://rubyplay.com/games/mad-hit-supernova/")

    def test_parser_fails_closed_on_duplicate_active_game_ids(self) -> None:
        duplicated = CSV_TEXT + (
            "Duplicate,Active,2024-01-01,rp_214,1,No,"
            "https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicado"):
            parse_official_game_list_csv(duplicated, provider_key="rubyplay")

    def test_parser_fails_closed_when_active_row_lacks_demo_link(self) -> None:
        broken = '''Name,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\nBroken,Active,2026-01-01,rp_777,5,Yes,\n'''
        with self.assertRaisesRegex(ValueError, "Demo Link"):
            parse_official_game_list_csv(broken, provider_key="rubyplay")


if __name__ == "__main__":
    unittest.main()
