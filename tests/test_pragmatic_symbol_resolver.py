from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_symbol_resolver import collect_symbol_candidates


class PragmaticSymbolResolverTests(unittest.TestCase):
    def test_gates_pop_rejects_mixed_case_page_token_and_uses_validated_hint(self) -> None:
        text = 'window.pageToken="csCAKfIodIBIyQJAGmPQTDHgrzca94K";'
        candidates = collect_symbol_candidates(
            text,
            "https://www.pragmaticplay.com/en/games/gates-of-olympus-pop/",
        )
        self.assertEqual([item[0] for item in candidates], ["vs10olymppop"])

    def test_normal_lowercase_game_symbol_is_retained(self) -> None:
        text = '{"gameSymbol":"vswaysrhino","cver":"123456"}'
        candidates = collect_symbol_candidates(
            text,
            "https://www.pragmaticplay.com/en/games/great-rhino-megaways/",
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0][0], "vswaysrhino")
        self.assertEqual(candidates[0][1], "123456")

    def test_launch_url_has_priority_over_page_fields(self) -> None:
        text = (
            'https://demogamesfree.pragmaticplay.net/gs2c/openGame.do?'
            'gameSymbol=vs20example&cver=654321 '
            '{"gameSymbol":"vs20other"}'
        )
        candidates = collect_symbol_candidates(
            text,
            "https://www.pragmaticplay.com/en/games/example/",
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0][0], "vs20example")


if __name__ == "__main__":
    unittest.main()
