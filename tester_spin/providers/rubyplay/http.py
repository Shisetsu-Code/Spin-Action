from __future__ import annotations

import ssl

import certifi
import requests
from requests.adapters import HTTPAdapter


class RubyPlaySystemTrustAdapter(HTTPAdapter):
    """HTTPS adapter using OS trust plus Requests' certifi bundle.

    RubyPlay may be reachable in a browser while Requests fails with
    CERTIFICATE_VERIFY_FAILED on Windows because Requests normally pins its
    verification to certifi instead of consulting certificates/intermediates
    available in the Windows trust stores.  We keep certificate and hostname
    verification enabled; this adapter only changes the trust source.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.ssl_context = build_rubyplay_ssl_context()
        super().__init__(*args, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        # Requests >=2.32 injects its own certifi-only SSLContext for verify=True.
        # Replace that context only for the normal verified path.  Explicit
        # verify=False or a caller-provided CA path retain Requests semantics.
        if verify is True:
            pool_kwargs["ssl_context"] = self.ssl_context
            pool_kwargs.pop("ca_certs", None)
            pool_kwargs.pop("ca_cert_dir", None)
        return host_params, pool_kwargs


def build_rubyplay_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # create_default_context/load_default_certs consult the Windows ROOT/CA
    # stores on Windows. Add certifi as well so RubyPlay sessions do not lose
    # the public roots normally available to Requests.
    context.load_verify_locations(cafile=certifi.where())
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def mount_rubyplay_system_trust(session: requests.Session) -> requests.Session:
    session.mount("https://", RubyPlaySystemTrustAdapter())
    return session


__all__ = [
    "RubyPlaySystemTrustAdapter",
    "build_rubyplay_ssl_context",
    "mount_rubyplay_system_trust",
]
