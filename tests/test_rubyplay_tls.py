from __future__ import annotations

import ssl
import unittest

import requests

from tester_spin.providers.rubyplay.adapter import RubyPlayProvider
from tester_spin.providers.rubyplay.http import (
    RubyPlaySystemTrustAdapter,
    build_rubyplay_ssl_context,
)


class RubyPlayTLSTests(unittest.TestCase):
    def test_context_keeps_certificate_and_hostname_verification_enabled(self) -> None:
        context = build_rubyplay_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_provider_session_mounts_system_trust_only_for_https(self) -> None:
        session = RubyPlayProvider._new_session()
        try:
            self.assertIs(session.verify, True)
            https_adapter = session.get_adapter("https://rubyplay.com/games/")
            http_adapter = session.get_adapter("http://rubyplay.com/games/")
            self.assertIsInstance(https_adapter, RubyPlaySystemTrustAdapter)
            self.assertIsInstance(http_adapter, requests.adapters.HTTPAdapter)
            self.assertNotIsInstance(http_adapter, RubyPlaySystemTrustAdapter)
        finally:
            session.close()

    def test_requests_default_context_is_replaced_only_for_verify_true(self) -> None:
        adapter = RubyPlaySystemTrustAdapter()
        request = requests.Request("GET", "https://rubyplay.com/games/").prepare()

        _host, verified = adapter.build_connection_pool_key_attributes(
            request,
            True,
        )
        self.assertIs(verified.get("ssl_context"), adapter.ssl_context)
        self.assertEqual(adapter.ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(adapter.ssl_context.check_hostname)

        _host, unverified = adapter.build_connection_pool_key_attributes(
            request,
            False,
        )
        self.assertIsNot(unverified.get("ssl_context"), adapter.ssl_context)
        self.assertEqual(unverified.get("cert_reqs"), "CERT_NONE")


if __name__ == "__main__":
    unittest.main()
