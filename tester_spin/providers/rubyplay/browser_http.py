from __future__ import annotations

from typing import Any

import requests


class RubyPlayVerifiedBrowserTransport:
    """Verified Chromium GET transport used only after requests TLS failure.

    TLS verification remains enabled: no ``ignore_https_errors`` and no
    certificate/hostname bypasses are configured.  Chromium is launched lazily
    on the first fallback request and is closed with the owning provider session.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None

    def _start(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128 Safari/537.36"
                ),
            )
        except Exception:
            self.close()
            raise

    def get(self, url: str, *, timeout_s: float) -> requests.Response:
        self._start()
        assert self._context is not None
        page = self._context.new_page()
        try:
            browser_response = page.goto(
                str(url),
                wait_until="domcontentloaded",
                timeout=max(5_000, int(float(timeout_s) * 1000)),
            )
            if browser_response is None:
                raise RuntimeError(f"RubyPlay Chromium GET sin respuesta para {url}")

            headers: dict[str, Any] = dict(browser_response.headers)
            content_type = str(headers.get("content-type") or "").lower()
            if "text/html" in content_type or "application/xhtml" in content_type:
                body = page.content().encode("utf-8")
            else:
                body = browser_response.body()

            response = requests.Response()
            response.status_code = int(browser_response.status)
            response.url = str(page.url or url)
            response.headers.update({str(k): str(v) for k, v in headers.items()})
            response._content = bytes(body)
            response.encoding = browser_response.headers.get("content-encoding") or "utf-8"
            return response
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = None
        self._browser = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


class RubyPlayTlsFallbackSession:
    """requests-shaped session with a lazy, verified Chromium GET fallback."""

    def __init__(self, base_session: Any) -> None:
        self._base = base_session
        self._browser: RubyPlayVerifiedBrowserTransport | None = None
        self.headers = base_session.headers

    def _browser_transport(self) -> RubyPlayVerifiedBrowserTransport:
        if self._browser is None:
            self._browser = RubyPlayVerifiedBrowserTransport()
        return self._browser

    @staticmethod
    def _timeout_seconds(kwargs: dict[str, Any]) -> float:
        raw = kwargs.get("timeout", 30.0)
        if isinstance(raw, tuple):
            raw = max(float(value) for value in raw if value is not None)
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    def get(self, url: str, **kwargs):
        try:
            return self._base.get(url, **kwargs)
        except requests.exceptions.SSLError:
            return self._browser_transport().get(
                str(url),
                timeout_s=self._timeout_seconds(kwargs),
            )

    def post(self, *args, **kwargs):
        return self._base.post(*args, **kwargs)

    def close(self) -> None:
        try:
            self._base.close()
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None

    def __getattr__(self, name: str):
        return getattr(self._base, name)


__all__ = ["RubyPlayTlsFallbackSession", "RubyPlayVerifiedBrowserTransport"]
