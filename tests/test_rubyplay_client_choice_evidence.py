from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.rubyplay.choice_client_evidence import (
    build_choice_client_evidence,
    write_choice_client_evidence,
)
from tester_spin.providers.rubyplay.runtime import bootstrap_game


class _Response:
    def __init__(self, *, text="", payload=None, url="", status_code=200):
        self.text = text
        self._payload = payload
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class _BootstrapSession:
    def __init__(self, public_url: str, launcher_url: str, bundle: str):
        self.public_url = public_url
        self.launcher_url = launcher_url
        self.bundle = bundle
        self.headers = {}

    def get(self, url, **kwargs):
        if url == self.public_url:
            return _Response(
                text=f'<iframe src="{self.launcher_url.replace("&", "&amp;")}"></iframe>',
                url=self.public_url,
            )
        if url == self.launcher_url:
            return _Response(
                text=(
                    '<script>window.RP_CONFIG={cdn:"https://cdn.example/"};</script>'
                    '<script src="https://cdn.example/game.js"></script>'
                ),
                url=self.launcher_url,
            )
        if url == "https://cdn.example/game.js":
            return _Response(text=self.bundle, url=url)
        if url == "https://srv.example/init-session/demo":
            return _Response(
                payload={
                    "status": "success",
                    "sessionKey": "secret",
                    "funModeData": {"gameId": 1, "sessionData": "opaque"},
                },
                url=url,
            )
        raise AssertionError(url)

    def post(self, url, *, json, timeout):
        self.assert_init = dict(json)
        if url != "https://srv.example/gameserver/demo":
            raise AssertionError(url)
        return _Response(
            payload={
                "status": "ok",
                "topic": "gameserver/init",
                "data": {
                    "an": 0,
                    "next_action": "spin",
                    "game_config": {"bets": [10], "def_bet_index": 0},
                    "player": {"balance": 100000, "currency": "EUR", "subunit": 100},
                },
                "funModeData": {"gameId": 1, "sessionData": "opaque"},
            },
            url=url,
        )


class RubyPlayChoiceClientEvidenceTests(unittest.TestCase):
    def test_strong_choice_handler_contexts_are_persisted_compactly(self) -> None:
        scripts = [
            (
                "https://cdn.example/game.js",
                (
                    "A" * 900
                    + "class PickMessageHandler{handle(index){return index<8}};"
                    + "B" * 900
                    + "class SelectMessageHandler{handle(index){return index>=0&&index<3}};"
                    + "C" * 900
                ),
            )
        ]

        evidence = build_choice_client_evidence(scripts)

        self.assertEqual(evidence["schema"], "tester-spin/rubyplay-choice-client-evidence/v1")
        self.assertEqual(len(evidence["scripts"]), 1)
        row = evidence["scripts"][0]
        self.assertEqual(row["url"], "https://cdn.example/game.js")
        self.assertEqual(len(row["sha256"]), 64)
        markers = {hit["marker"] for hit in row["hits"]}
        self.assertEqual(markers, {"PickMessageHandler", "SelectMessageHandler"})
        self.assertTrue(all(len(hit["excerpt"]) <= 1400 for hit in row["hits"]))
        self.assertTrue(all(len(hit["excerpt_sha256"]) == 64 for hit in row["hits"]))

    def test_generic_action_literal_without_index_context_is_not_promoted_as_handler_evidence(self) -> None:
        evidence = build_choice_client_evidence(
            [
                (
                    "https://cdn.example/game.js",
                    "const labels=['pick','select']; function render(){return labels.join(',')}",
                )
            ]
        )

        self.assertEqual(evidence["scripts"][0]["hits"], [])

    def test_bootstrap_persists_choice_client_evidence_from_downloaded_scripts(self) -> None:
        public_url = "https://rubyplay.example/games/g/"
        launcher_url = (
            "https://launcher.example/launcher?gamename=rp_test"
            "&operator=rubyplay.com&server_url=https://srv.example"
            "&currency=EUR&mode=fun&lang=en"
        )
        bundle = (
            'tt.VERSION=2;'
            'tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'var X={};X.MATH_VERSION=2026010196;X.WAGER=10;'
            'class PickMessageHandler{handle(index){return index<8}}'
        )
        session = _BootstrapSession(public_url, launcher_url, bundle)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = bootstrap_game(
                session,
                public_url,
                timeout_s=5.0,
                artifact_dir=root,
            )
            artifact = root / "client-choice-evidence.json"
            self.assertTrue(artifact.is_file())
            saved = json.loads(artifact.read_text(encoding="utf-8"))

        self.assertEqual(runtime.client_profile.protocol_version, 2)
        self.assertEqual(saved["summary"]["scripts_with_choice_evidence"], 1)
        self.assertEqual(saved["scripts"][0]["hits"][0]["marker"], "PickMessageHandler")

    def test_writer_creates_machine_readable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_choice_client_evidence(
                root,
                [
                    (
                        "https://cdn.example/game.js",
                        "class PickMessageHandler{handle(index){return index<4}}",
                    )
                ],
            )
            self.assertEqual(path, root / "client-choice-evidence.json")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["scripts"][0]["hits"][0]["marker"], "PickMessageHandler")


if __name__ == "__main__":
    unittest.main()
