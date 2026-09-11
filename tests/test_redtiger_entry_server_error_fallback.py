from __future__ import annotations

import inspect
import unittest

from tester_spin.providers.redtiger import bootstrap_browser


class RedTigerEntryServerErrorFallbackTests(unittest.TestCase):
    def test_any_http_entry_failure_can_trigger_live_embedded_fallback(self) -> None:
        source = inspect.getsource(bootstrap_browser.bootstrap_game)
        self.assertIn('int(getattr(response, "status", 0) or 0) >= 400', source)
        self.assertIn("entryEmbedded anunciado por token/demo", source)
        self.assertNotIn("if int(getattr(response, \"status\", 0) or 0) in {401, 403}", source)


if __name__ == "__main__":
    unittest.main()
