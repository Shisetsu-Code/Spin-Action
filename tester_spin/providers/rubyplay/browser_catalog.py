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

    Playwright keeps its normal certificate and hostname verification.  This
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
        """Return the rendered catalogue HTML from a verified browser session."""
        self.start()
        assert self._page is not None
        return str(self._page.content() or ""), str(self._page.url or self.catalog_url)

    def fetch_page(self, state: BricksCatalogState, page: int) -> BrowserBricksResponse:
        self.start()
        assert self._page is not None

        fallback_payload = load_query_payload(state, page)
        result = self._page.evaluate(
            """
            async ({fallbackUrl, fallbackPayload, fallbackWpRestNonce}) => {
              const data = window.bricksData || {};
              const restRoot = String(data.restApiUrl || fallbackUrl || '');
              const endpoint = restRoot.endsWith('/')
                ? `${restRoot}load_query_page`
                : `${restRoot}/load_query_page`;

              const payload = Object.assign({}, fallbackPayload, {
                postId: data.postId || fallbackPayload.postId,
                nonce: data.nonce || fallbackPayload.nonce,
                lang: data.language || fallbackPayload.lang,
              });

              const url = new URL(endpoint, window.location.href);
              if (payload.lang) url.searchParams.set('lang', payload.lang);

              const headers = {
                'Content-Type': 'application/json; charset=UTF-8',
                'Accept': 'application/json, text/plain, */*',
              };
              const wpRestNonce = data.wpRestNonce || fallbackWpRestNonce || '';
              if (wpRestNonce) headers['X-WP-Nonce'] = wpRestNonce;

              const response = await fetch(url.toString(), {
                method: 'POST',
                headers,
                credentials: 'same-origin',
                body: JSON.stringify(payload),
              });
              return {
                status: response.status,
                url: response.url,
                body: await response.text(),
                noncePresent: !!data.nonce,
                wpRestNoncePresent: !!wpRestNonce,
              };
            }
            """,
            {
                "fallbackUrl": state.rest_api_url,
                "fallbackPayload": fallback_payload,
                "fallbackWpRestNonce": state.wp_rest_nonce,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("RubyPlay browser Bricks: respuesta de fetch inválida.")

        status = int(result.get("status") or 0)
        body = str(result.get("body") or "")
        url = str(result.get("url") or state.load_query_url)
        if status < 200 or status >= 300:
            excerpt = " ".join(body.split())[:400]
            raise RuntimeError(
                f"RubyPlay browser Bricks HTTP {status} para {url}: {excerpt}"
            )

        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise ValueError(
                f"RubyPlay browser Bricks: respuesta no JSON ({type(exc).__name__})."
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("RubyPlay browser Bricks: respuesta JSON no es objeto.")

        return BrowserBricksResponse(
            status=status,
            url=url,
            body=body,
            data=parsed,
        )
