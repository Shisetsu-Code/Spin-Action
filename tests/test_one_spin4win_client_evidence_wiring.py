from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game
from tester_spin.providers.one_spin4win import OneSpin4WinProvider


class _FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class _FakeCookies:
    def get_dict(self) -> dict[str, str]:
        return {}


class _FakeSession:
    cookies = _FakeCookies()

    def __init__(self, game_url: str) -> None:
        self.game_url = game_url

    def get(self, url: str, **_kwargs):
        if url == self.game_url:
            return _FakeResponse(
                url,
                '<html><script src="/loader.js"></script></html>',
            )
        if url.endswith("/loader.js"):
            return _FakeResponse(
                url,
                'this.gameURL="wss://game.1spin4win.games/ws";'
                'addJSFile("/game.js");',
            )
        if url.endswith("/game.js"):
            return _FakeResponse(
                url,
                'this.gameController.connect('
                '"ClassicChilliPlus","testuser2","debug","","01","","");'
                'this.gameController.playGame(lines, betIndex, 0);'
                'const finish = gameController["finishRound"];',
            )
        raise AssertionError(f"unexpected URL: {url}")


class OneSpin4WinClientEvidenceWiringTests(unittest.TestCase):
    def test_runtime_spec_persists_neutral_client_action_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = OneSpin4WinProvider(Path(tmp))
            game = Game(
                provider=provider.key,
                slug="classic-chilli-plus",
                name="Classic Chilli Plus",
                url=(
                    "https://gs.1spin4win.com:10443/gmh5/classicchilliplus.html?"
                    "currency=EUR&config=1&freeplay=true&language=en&exit=none"
                ),
                symbol="classicchilliplus",
            )
            provider._worker_session = lambda: _FakeSession(game.url)  # type: ignore[method-assign]

            attempt_dir = Path(tmp) / "attempt"
            spec = provider._discover_runtime_spec(
                game,
                timeout_s=5.0,
                attempt_dir=attempt_dir,
            )

            evidence_path = attempt_dir / "client-action-evidence.json"
            self.assertTrue(evidence_path.is_file())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertIn("client_action_evidence", spec)
        self.assertEqual(
            evidence["aggregate"]["game_controller_methods"],
            ["connect", "playGame"],
        )
        self.assertEqual(
            evidence["aggregate"]["game_controller_references"],
            ["connect", "finishRound", "playGame"],
        )
        self.assertEqual(spec["client_action_evidence"], evidence["aggregate"])


if __name__ == "__main__":
    unittest.main()
