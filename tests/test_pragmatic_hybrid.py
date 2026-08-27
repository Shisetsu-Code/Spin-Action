from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.providers.pragmatic_hybrid import PragmaticProvider


class PragmaticHybridTests(unittest.TestCase):
    def test_catalog_uses_har_grounded_ajax_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            stop_event = threading.Event()
            logs: list[str] = []
            with patch(
                "tester_spin.providers.pragmatic_hybrid.crawl_pragmatic_catalog_ajax",
                return_value=[],
            ) as crawl:
                result = provider.crawl_catalog(
                    stop_event=stop_event,
                    progress=logs.append,
                    max_pages=7,
                )

        self.assertEqual(result, [])
        crawl.assert_called_once()
        self.assertIs(crawl.call_args.args[0], provider)
        self.assertEqual(crawl.call_args.kwargs["max_pages"], 7)
        self.assertTrue(any("v6 AJAX" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
