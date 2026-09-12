from __future__ import annotations

import inspect
import unittest

from tester_spin.providers.redtiger import bootstrap_browser


class RedTigerEvolutionGamesBootstrapTests(unittest.TestCase):
    def test_active_bootstrap_uses_official_evolution_games_start_form(self) -> None:
        source = inspect.getsource(bootstrap_browser.bootstrap_game)
        self.assertIn('games.evolution.com', source)
        self.assertIn('_wait_for_official_loader', source)
        self.assertIn('_submit_official_start', source)
        self.assertIn('platform/game/settings', source)
        self.assertNotIn('demo_page_url(', source)
        self.assertNotIn('entryEmbedded anunciado por token/demo', source)

    def test_start_response_is_the_observed_wordpress_game_start_endpoint(self) -> None:
        source = inspect.getsource(bootstrap_browser._is_start_response)
        self.assertIn('/wp-json/games/v1/start', source)
        self.assertIn('games.evolution.com', source)


if __name__ == "__main__":
    unittest.main()
