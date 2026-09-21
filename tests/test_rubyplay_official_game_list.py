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

CURRENT_WIDE_CSV = '''Name,,Status,Release Date,Game ID,Wager,Max Win Multiplier,BF Max Win Multiplier,Default RTP,Only Buy Feature RTP,Volatility in %,Volatility,Hit Rate Frequency,Free Rounds,Buy Feature,Awarded Feature Support,Theme,Features,Demo Link\nMad Hit Mr Coin SE 96,,Upcoming,2026-11-12,rp_240,2,x3728,x3583,96.32%,96%,81.63,5,16.99%,Active,Yes,Yes,Banking/Money,"Mad Hit Instant Win, Mad Hit Collect and Win",https://demo.rubyplay.com/launcher?gamename=rp_240&mode=offline\nJ Mania Chili Champs 96,,Active,2026-09-03,rp_214,5,5000x,5000x,96.30%,96%,91%,5,56.70%,Active,Yes,Yes,Chili/Spices,"Wild Surge, J Mania, Jackpot Pick",https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline\n'''


class RubyPlayOfficialGameListTests(unittest.TestCase):
    def test_discovers_google_sheet_and_builds_csv_export_url(self) -> None:
        sheet = discover_game_list_sheet_url(DOC_HTML, "https://docs.rubyplay.com/content/integration/lists/game-list")
        self.assertEqual(
            sheet,
            "https://docs.google.com/spreadsheets/d/1AbC_def-123/edit?gid=987654321",
        )
        self.assertEqual(
            build_sheet_csv_url(sheet),
            "https://docs.google.com/spreadsheets/d/1AbC_def-123/gviz/tq?"
            "tqx=out%3Acsv&gid=987654321&range=A2%3AT&headers=1",
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

    def test_parser_accepts_current_wide_sheet_with_blank_column_b(self) -> None:
        records = parse_official_game_list_csv(CURRENT_WIDE_CSV, provider_key="rubyplay")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.game.symbol, "rp_214")
        self.assertEqual(record.game.slug, "rp_214")
        self.assertEqual(
            record.game.url,
            "https://demo.rubyplay.com/launcher?gamename=rp_214&mode=offline",
        )
        self.assertTrue(record.buy_feature)
        self.assertEqual(record.default_rtp, "96.30%")
        self.assertEqual(record.theme, "Chili/Spices")
        self.assertIn("J Mania", record.features)

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

    def test_missing_header_error_contains_only_bounded_row_preview(self) -> None:
        broken = (
            "Unexpected A,Unexpected B,Unexpected C\n"
            "Value A,Value B,Value C\n"
            + ("x" * 500)
            + ",secret-looking-value\n"
        )
        with self.assertRaises(ValueError) as caught:
            parse_official_game_list_csv(broken, provider_key="rubyplay")
        message = str(caught.exception)
        self.assertIn("primeras_filas=", message)
        self.assertIn("Unexpected A", message)
        self.assertIn("Value A", message)
        self.assertNotIn("secret-looking-value", message)
        self.assertLess(len(message), 600)


    def test_invalid_game_id_diagnostic_is_bounded_and_hides_query_values(self) -> None:
        broken = (
            "Name,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\n"
            "Voltage Blitz Rapid Boost 94,Active,2026-09-10,koala_123,1,Yes,"
            "https://demo.rubyplay.com/launcher?gamename=koala_123&mode=offline&token=secret-value\n"
        )
        with self.assertRaises(ValueError) as caught:
            parse_official_game_list_csv(broken, provider_key="rubyplay")
        message = str(caught.exception)
        self.assertIn("koala_123", message)
        self.assertIn("demo.rubyplay.com", message)
        self.assertIn("/launcher", message)
        self.assertIn("token", message)
        self.assertNotIn("secret-value", message)
        self.assertLess(len(message), 700)


    def test_parser_accepts_namespaced_official_game_id_when_launcher_matches(self) -> None:
        text = (
            "Name,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\n"
            "Voltage Blitz Rapid Boost 94,Active,2026-09-10,kg_5025,1,Yes,"
            "https://demo.rubyplay.com/launcher?gamename=kg_5025&mode=offline\n"
        )
        records = parse_official_game_list_csv(text, provider_key="rubyplay")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].game.symbol, "kg_5025")
        self.assertEqual(records[0].game.slug, "kg_5025")

    def test_namespaced_official_game_id_still_requires_exact_launcher_identity(self) -> None:
        text = (
            "Name,Status,Release Date,Game ID,Wager,Buy Feature,Demo Link\n"
            "Mismatch,Active,2026-09-10,kg_5025,1,Yes,"
            "https://demo.rubyplay.com/launcher?gamename=kg_9999&mode=offline\n"
        )
        with self.assertRaisesRegex(ValueError, "no coincide"):
            parse_official_game_list_csv(text, provider_key="rubyplay")


if __name__ == "__main__":
    unittest.main()
