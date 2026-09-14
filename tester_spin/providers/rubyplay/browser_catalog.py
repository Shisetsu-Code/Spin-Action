from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tester_spin.providers.rubyplay.catalog import BricksCatalogState, load_query_payload


@dataclass(slots=True)
class BrowserBricksResponse:
    status: int
    url: str
    body: str
    data: dict[str, Any]


class RubyPlayBrowserCatalogClient:
    """Lazy Playwright transport for RubyPlay catalogue requests.

    The normal catalogue path stays HTTP-first. This client is started only
    when the direct requests transport cannot establish a verified TLS session
    or when RubyPlay rejects a direct Bricks ``load_query_page`` replay.

    Playwright keeps its normal certificate and hostname verification. This
    class never enables ``ignore_https_errors`` and never weakens TLS checks.
    """

    def __init__(self, catalog_url: str, *, timeout_s: float = 30.0) -> None:
        self.catalog_url = catalog_url
        self.timeout_ms = max(5_000, int(float(timeout_s) * 1000))
        self._playwright = None
        self._browser = None
        self._page = None

    def start(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._page = self._browser.new_page(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128 Safari/537.36"
                ),
            )
            self._page.goto(
                self.catalog_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            self._page.wait_for_function(
                "() => !!(window.bricksData && window.bricksData.nonce)",
                timeout=self.timeout_ms,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        page, browser, playwright = self._page, self._browser, self._playwright
        self._page = None
        self._browser = None
        self._playwright = None
        if page is not None:
            try:
                page.close()
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

    def fetch_catalog_html(self) -> tuple[str, str]:
        self.start()
        assert self._page is not None
        return str(self._page.content() or ""), str(self._page.url or self.catalog_url)

    def request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> BrowserBricksResponse:
        """Execute a same-origin JSON request from the verified browser page."""
        self.start()
        assert self._page is not None
        result = self._page.evaluate(
            """
            async ({target, query, payload, headers}) => {
              const data = window.bricksData || {};
              const u = new URL(target, window.location.href);
              for (const [key, value] of Object.entries(query || {})) {
                if (value !== null && value !== undefined && String(value) !== '') {
                  u.searchParams.set(key, String(value));
                }
              }
              const body = Object.assign({}, payload || {});
              if (Object.prototype.hasOwnProperty.call(body, 'nonce') && data.nonce) {
                body.nonce = data.nonce;
              }
              if (Object.prototype.hasOwnProperty.call(body, 'postId') && data.postId) {
                body.postId = data.postId;
              }
              if (Object.prototype.hasOwnProperty.call(body, 'lang') && data.language) {
                body.lang = data.language;
              }
              const requestHeaders = Object.assign(
                {'Content-Type': 'application/json; charset=UTF-8', 'Accept': 'application/json, text/plain, */*'},
                headers || {},
              );
              const wpRestNonce = data.wpRestNonce || requestHeaders['X-WP-Nonce'] || '';
              if (wpRestNonce) requestHeaders['X-WP-Nonce'] = wpRestNonce;
              const response = await fetch(u.toString(), {
                method: 'POST',
                headers: requestHeaders,
                credentials: 'same-origin',
                body: JSON.stringify(body),
              });
              return {status: response.status, url: response.url, body: await response.text()};
            }
            """,
            {
                "target": str(url),
                "query": dict(params or {}),
                "payload": dict(payload or {}),
                "headers": dict(headers or {}),
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("RubyPlay browser request: respuesta inválida.")
        status = int(result.get("status") or 0)
        body = str(result.get("body") or "")
        resolved_url = str(result.get("url") or url)
        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise ValueError(
                f"RubyPlay browser request: respuesta no JSON ({type(exc).__name__})."
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("RubyPlay browser request: respuesta JSON no es objeto.")
        return BrowserBricksResponse(
            status=status,
            url=resolved_url,
            body=body,
            data=parsed,
        )

    def fetch_page(self, state: BricksCatalogState, page: int) -> BrowserBricksResponse:
        fallback_payload = load_query_payload(state, page)
        return self.request_json(
            state.load_query_url,
            params={"lang": state.language},
            payload=fallback_payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.catalog_url,
            },
        )
