from __future__ import annotations

import unittest
from unittest.mock import patch

from tester_spin.providers.rubyplay.launcher_params import parse_launcher_url
from tester_spin.providers.rubyplay.launcher_resolver import (
    extract_executable_launcher_url,
)


FINAL = (
    "https://prrpeu3.com/launcher?gamename=rp_71&operator=rubyplay.com"
    "&server_url=https://srv.prrpeu3.com&currency=EUR&mode=fun&lang=en"
)
INCOMPLETE = (
    "https://prrpeu3.com/launcher?gamename=rp_71&operator=rubyplay.com"
    "&server_url=https://srv.prrpeu3.com&mode=fun&lang=en"
)


class RubyPlayLauncherResolutionTests(unittest.TestCase):
    def test_complete_demo_launcher_is_taken_from_static_page(self) -> None:
        html = (
            f'<iframe src="{INCOMPLETE.replace("&", "&amp;")}"></iframe>'
            f'<a href="{FINAL.replace("&", "&amp;")}">Play Demo</a>'
        )
        with patch(
            "tester_spin.providers.rubyplay.launcher_resolver.resolve_demo_launcher_browser",
            side_effect=AssertionError("browser should not be needed"),
        ):
            self.assertEqual(
                extract_executable_launcher_url(html, "https://rubyplay.com/games/x/"),
                FINAL,
            )

    def test_incomplete_placeholder_falls_back_to_play_demo_browser(self) -> None:
        html = f'<iframe src="{INCOMPLETE.replace("&", "&amp;")}"></iframe>'
        with patch(
            "tester_spin.providers.rubyplay.launcher_resolver.resolve_demo_launcher_browser",
            return_value=FINAL,
        ) as browser:
            resolved = extract_executable_launcher_url(
                html,
                "https://rubyplay.com/games/alice-in-the-wild/",
            )
        self.assertEqual(resolved, FINAL)
        browser.assert_called_once()

    def test_user_observed_launcher_format_is_complete_and_parseable(self) -> None:
        parsed = parse_launcher_url(FINAL)
        self.assertEqual(parsed.gamename, "rp_71")
        self.assertEqual(parsed.operator, "rubyplay.com")
        self.assertEqual(parsed.server_url, "https://srv.prrpeu3.com")
        self.assertEqual(parsed.currency, "EUR")
        self.assertEqual(parsed.mode, "fun")
        self.assertEqual(parsed.lang, "en")


if __name__ == "__main__":
    unittest.main()
