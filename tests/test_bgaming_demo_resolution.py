from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.runtime import is_demo_url, resolve_fresh_demo_url


class _Response:
    def __init__(self, url: str, text: str, status_code: int = 200) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, response: _Response | None = None, responses: dict[str, _Response] | None = None) -> None:
        self.response = response
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, bool]] = []

    def get(self, url: str, *, timeout: float, allow_redirects: bool = False):
        self.calls.append((url, allow_redirects))
        if url in self.responses:
            return self.responses[url]
        if self.response is None:
            raise AssertionError(f"unexpected GET: {url}")
        return self.response


class BGamingDemoResolutionTests(unittest.TestCase):
    def test_hyperhive_runtime_with_launch_token_is_a_demo_url(self) -> None:
        self.assertTrue(
            is_demo_url(
                "https://game.demo.bgaming-network.com/hyperhive?launch_token=abc"
            )
        )

    def test_hyperhive_without_runtime_token_is_not_accepted(self) -> None:
        self.assertFalse(
            is_demo_url("https://game.demo.bgaming-network.com/hyperhive")
        )

    def test_non_bgaming_hyperhive_url_is_not_accepted(self) -> None:
        self.assertFalse(
            is_demo_url("https://example.com/hyperhive?launch_token=abc")
        )

    def test_expected_identifier_skips_unrelated_demo_candidate(self) -> None:
        public = "https://bgaming.com/games/alice-wonderluck"
        wrong = "https://demo.bgaming-network.com/play/Wrong/1/session"
        alice = "https://demo.bgaming-network.com/play/AliceWonderLuck/1/session"
        page = _Response(
            public,
            f'<a href="{wrong}">wrong</a><a href="{alice}">alice</a>',
        )
        wrong_html = """
        <script>window.__OPTIONS__ = {
          "identifier":"WrongGame",
          "api":"https://demo.bgaming-network.com/api/WrongGame/1/session",
          "csrfTokenHeaderName":"X-CSRF-Token",
          "csrfTokenHeaderValue":"secret"
        };</script>
        """
        alice_html = """
        <script>window.__OPTIONS__ = {
          "identifier":"AliceWonderLuck",
          "api":"https://demo.bgaming-network.com/api/AliceWonderLuck/1/session",
          "csrfTokenHeaderName":"X-CSRF-Token",
          "csrfTokenHeaderValue":"secret"
        };</script>
        """
        session = _Session(
            responses={
                public: page,
                wrong: _Response(wrong, wrong_html),
                alice: _Response(alice, alice_html),
            }
        )

        resolved = resolve_fresh_demo_url(
            session,
            public,
            timeout_s=2,
            expected_identifier="AliceWonderLuck",
        )

        self.assertEqual(resolved, alice)

    def test_direct_demo_identifier_mismatch_is_rejected(self) -> None:
        demo = "https://demo.bgaming-network.com/play/Wrong/1/session"
        html = """
        <script>window.__OPTIONS__ = {
          "identifier":"WrongGame",
          "api":"https://demo.bgaming-network.com/api/WrongGame/1/session",
          "csrfTokenHeaderName":"X-CSRF-Token",
          "csrfTokenHeaderValue":"secret"
        };</script>
        """
        session = _Session(_Response(demo, html))

        resolved = resolve_fresh_demo_url(
            session,
            demo,
            timeout_s=2,
            expected_identifier="AliceWonderLuck",
        )

        self.assertEqual(resolved, "")
        self.assertEqual(session.calls, [(demo, True)])

    def test_public_page_redirect_to_hyperhive_is_returned_when_bootstrap_is_valid(self) -> None:
        final_url = (
            "https://game.demo.bgaming-network.com/hyperhive?launch_token=abc"
        )
        html = """
        <html><head><script>
        window.__OPTIONS__ = {
          "identifier":"Example",
          "api":"https://game.demo.bgaming-network.com/api/Example/1/session",
          "csrfTokenHeaderName":"X-CSRF-Token",
          "csrfTokenHeaderValue":"secret",
          "play_token":"play-secret"
        };
        </script></head></html>
        """
        session = _Session(_Response(final_url, html))

        resolved = resolve_fresh_demo_url(
            session,
            "https://bgaming.com/games/example",
            timeout_s=2,
        )

        self.assertEqual(resolved, final_url)
        self.assertEqual(
            session.calls,
            [("https://bgaming.com/games/example", True)],
        )


if __name__ == "__main__":
    unittest.main()
