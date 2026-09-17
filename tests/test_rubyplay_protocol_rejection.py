from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.runtime import LauncherConfig, RubyPlayClientProfile, RubyPlayRuntime
from tester_spin.providers.rubyplay.runtime_contracts import RubyPlayProtocolRejection, post_action


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "error",
            "topic": "gameserver/select",
            "error": "invalid index",
            "data": {"next_action": "select", "an": 4},
        }


class _Session:
    def post(self, url, *, json, timeout):
        return _Response()


def _runtime() -> RubyPlayRuntime:
    return RubyPlayRuntime(
        session=_Session(),  # type: ignore[arg-type]
        launcher=LauncherConfig(
            launcher_url="https://example.invalid/launcher?gamename=rp_test&operator=x&server_url=https://srv.example.invalid&currency=EUR&mode=fun&lang=en",
            gamename="rp_test",
            operator="x",
            server_url="https://srv.example.invalid",
            currency="EUR",
            mode="fun",
            lang="en",
        ),
        client_profile=RubyPlayClientProfile(
            protocol_version=2,
            math_version=123,
            actions=["spin", "select"],
        ),
        session_key="secret",
        fun_mode_data={"gameId": 1},
        init_data={},
        bets=[10],
        default_bet=10,
        default_bet_index=0,
        currency="EUR",
        subunit=100,
        action_number=3,
        next_action="select",
    )


class RubyPlayProtocolRejectionTests(unittest.TestCase):
    def test_provider_status_error_is_typed_semantic_rejection(self) -> None:
        runtime = _runtime()
        with self.assertRaises(RubyPlayProtocolRejection) as caught:
            post_action(runtime, "select", timeout_s=5.0, action_index=2)

        rejection = caught.exception
        self.assertEqual(rejection.action, "select")
        self.assertEqual(rejection.provider_status, "error")
        self.assertEqual(rejection.payload["error"], "invalid index")
        self.assertEqual(runtime.action_number, 3)
        self.assertEqual(runtime.next_action, "select")


if __name__ == "__main__":
    unittest.main()
