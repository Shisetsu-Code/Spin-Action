from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.rubyplay.adapter import RubyPlayProvider
from tester_spin.providers.rubyplay.browser_catalog import BrowserBricksResponse
from tester_spin.providers.rubyplay.catalog import (
    load_query_payload,
    parse_bricks_catalog_state,
    parse_bricks_catalog_states,
    parse_catalog_html,
    query_loop_html,
    updated_query_element_id,
    updated_query_meta,
)


class _CatalogResponse:
    def __init__(self, *, url: str, text: str = "", payload=None, status_code: int = 200):
        self.url = url
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = self.url
            response._content = self.text.encode("utf-8")
            raise requests.HTTPError(
                f"{self.status_code} error for {self.url}",
                response=response,
            )

    def json(self):
        return self._payload


class _CatalogSession:
    def __init__(self, initial_html: str, *, forbidden: bool = False):
        self.initial_html = initial_html
        self.forbidden = forbidden
        self.posts: list[tuple[str, int]] = []

    def get(self, url, **_kwargs):
        return _CatalogResponse(url=url, text=self.initial_html)

    def post(self, url, *, params, json, headers, timeout):
        del params, headers, timeout
        query_id = str(json["queryElementId"])
        page = int(json["page"])
        self.posts.append((query_id, page))
        if page != 2:
            raise AssertionError(f"unexpected page {page}")
        if self.forbidden:
            return _CatalogResponse(
                url=url,
                status_code=403,
                text=(
                    '{"code":"rest_cookie_invalid_nonce",'
                    '"message":"Bricks cookie check failed",'
                    '"data":{"status":403}}'
                ),
            )
        slug = "alpha-2" if query_id == "alpha" else "beta-2"
        return _CatalogResponse(
            url=url,
            payload={
                "html": f'<div><a href="/games/{slug}/">{slug}</a></div>',
                "updated_query": {
                    "element_id": query_id,
                    "count": 2,
                    "max_num_pages": 2,
                    "start": 2,
                    "end": 2,
                },
            },
        )


class _FakeBrowserCatalogClient:
    instances = []

    def __init__(self, catalog_url: str, *, timeout_s: float = 30.0):
        self.catalog_url = catalog_url
        self.timeout_s = timeout_s
        self.started = False
        self.closed = False
        self.calls: list[tuple[str, int]] = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def fetch_page(self, state, page: int) -> BrowserBricksResponse:
        self.calls.append((state.query_element_id, page))
        slug = f"{state.query_element_id}-{page}"
        data = {
            "html": f'<div><a href="/games/{slug}/">{slug}</a></div>',
            "updated_query": {
                "element_id": state.query_element_id,
                "count": 2,
                "max_num_pages": 2,
                "start": 2,
                "end": 2,
            },
        }
        return BrowserBricksResponse(
            status=200,
            url=state.load_query_url,
            body="{}",
            data=data,
        )


class RubyPlayCatalogTests(unittest.TestCase):
    def test_bricks_query_is_found_by_post_type_not_element_id(self) -> None:
        html = r'''
        <script>
        var bricksData = {
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en",
          nested: {answer: 42}
        };
        </script>
        <div class="brx-query-trail"
             data-query-element-id="generated-id-123"
             data-query-vars='{"post_type":["games"],"posts_per_page":32,"paged":1}'
             data-page="1" data-max-pages="6"
             data-start="1" data-end="32"></div>
        '''
        state = parse_bricks_catalog_state(
            html,
            "https://rubyplay.com/games/",
        )
        self.assertEqual(state.query_element_id, "generated-id-123")
        self.assertEqual(state.max_pages, 6)
        self.assertEqual(state.query_vars["post_type"], ["games"])
        self.assertEqual(
            state.rest_api_url,
            "https://rubyplay.com/wp-json/bricks/v1/",
        )
        self.assertEqual(state.nonce, "query-nonce")
        payload = load_query_payload(state, 2)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["queryElementId"], "generated-id-123")
        self.assertIn('"post_type":["games"]', payload["queryVars"])

    def test_multiple_game_queries_are_all_discovered_not_ranked(self) -> None:
        trails = []
        loops = []
        specs = [
            ("ejqzkz", 10, 2),
            ("vdhuwp", 10, 2),
            ("iluwmw", 10, 1),
            ("yqwrlh", 10, 1),
            ("betbqo", 10, 1),
            ("ricfbf", 10, 1),
            ("hgbpso", 5, 1),
            ("ybnxtw", 4, 1),
        ]
        for index, (query_id, page_size, max_pages) in enumerate(specs):
            trails.append(
                f'''<div class="brx-query-trail"
                    data-query-element-id="{query_id}"
                    data-query-vars='{{"post_type":["games"],"posts_per_page":{page_size},"paged":1}}'
                    data-page="1" data-max-pages="{max_pages}"
                    data-start="1" data-end="{page_size}"></div>'''
            )
            loops.append(
                f'''<!--brx-loop-start-{query_id}-->
                <div class="brxe-{query_id}"><a href="/games/game-{index}/">Game {index}</a></div>
                <!--brx-loop-end-{query_id}-->'''
            )

        html = f'''
        <script>
        window.bricksData = {{
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en"
        }};
        </script>
        {''.join(trails)}
        {''.join(loops)}
        '''

        states = parse_bricks_catalog_states(html, "https://rubyplay.com/games/")
        self.assertEqual(len(states), 8)
        self.assertEqual(
            [state.query_element_id for state in states],
            [item[0] for item in specs],
        )
        self.assertEqual(states[0].max_pages, 2)
        self.assertEqual(states[1].max_pages, 2)
        self.assertTrue(all(state.candidate_count == 8 for state in states))

        all_slugs = []
        for state in states:
            fragment = query_loop_html(html, state.query_element_id)
            records = parse_catalog_html(fragment, "https://rubyplay.com/games/")
            all_slugs.extend(item.game.slug for item in records)
        self.assertEqual(len(set(all_slugs)), 8)

        with self.assertRaisesRegex(ValueError, "múltiples queries games"):
            parse_bricks_catalog_state(html, "https://rubyplay.com/games/")

    def test_zero_limit_crawls_every_page_of_every_game_loop(self) -> None:
        html = r'''
        <script>
        const bricksData = {
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en"
        };
        </script>
        <div data-query-element-id="alpha"
             data-query-vars='{"post_type":["games"],"posts_per_page":1,"paged":1}'
             data-page="1" data-max-pages="2" data-start="1" data-end="1"></div>
        <div data-query-element-id="beta"
             data-query-vars='{"post_type":["games"],"posts_per_page":1,"paged":1}'
             data-page="1" data-max-pages="2" data-start="1" data-end="1"></div>
        <!--brx-loop-start-alpha-->
          <div><a href="/games/alpha-1/">Alpha 1</a></div>
        <!--brx-loop-end-alpha-->
        <!--brx-loop-start-beta-->
          <div><a href="/games/beta-1/">Beta 1</a></div>
        <!--brx-loop-end-beta-->
        '''
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            fake = _CatalogSession(html)
            provider.http = fake  # type: ignore[assignment]
            games = provider.crawl_catalog(
                stop_event=threading.Event(),
                progress=lambda _message: None,
                max_pages=0,
            )

        self.assertEqual(
            {game.slug for game in games},
            {"alpha-1", "alpha-2", "beta-1", "beta-2"},
        )
        self.assertEqual(fake.posts, [("alpha", 2), ("beta", 2)])
        self.assertTrue(provider.catalog_crawl_authoritative)

    def test_403_pagination_retries_from_browser_origin(self) -> None:
        html = r'''
        <script>
        var bricksData = {
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en"
        };
        </script>
        <div data-query-element-id="alpha"
             data-query-vars='{"post_type":["games"],"posts_per_page":1,"paged":1}'
             data-page="1" data-max-pages="2" data-start="1" data-end="1"></div>
        <!--brx-loop-start-alpha-->
          <div><a href="/games/alpha-1/">Alpha 1</a></div>
        <!--brx-loop-end-alpha-->
        '''
        _FakeBrowserCatalogClient.instances.clear()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            fake = _CatalogSession(html, forbidden=True)
            provider.http = fake  # type: ignore[assignment]
            with patch(
                "tester_spin.providers.rubyplay.adapter.RubyPlayBrowserCatalogClient",
                _FakeBrowserCatalogClient,
            ):
                games = provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=messages.append,
                    max_pages=0,
                )

        self.assertEqual({game.slug for game in games}, {"alpha-1", "alpha-2"})
        self.assertEqual(fake.posts, [("alpha", 2)])
        self.assertEqual(len(_FakeBrowserCatalogClient.instances), 1)
        browser = _FakeBrowserCatalogClient.instances[0]
        self.assertTrue(browser.started)
        self.assertTrue(browser.closed)
        self.assertEqual(browser.calls, [("alpha", 2)])
        self.assertTrue(provider.catalog_crawl_authoritative)
        self.assertTrue(any("HTTP 403" in message for message in messages))
        self.assertTrue(any("fallback Chromium activo" in message for message in messages))

    def test_rest_root_can_be_derived_from_wordpress_link(self) -> None:
        html = r'''
        <link rel="https://api.w.org/" href="https://rubyplay.com/wp-json/" />
        <div data-query-element-id="x"
             data-query-vars='{"post_type":["games"],"posts_per_page":4}'
             data-page="1" data-max-pages="1" data-start="1" data-end="4"></div>
        '''
        state = parse_bricks_catalog_state(html, "https://rubyplay.com/games/")
        self.assertEqual(
            state.rest_api_url,
            "https://rubyplay.com/wp-json/bricks/v1/",
        )

    def test_cards_use_games_namespace_and_lazy_thumbnail(self) -> None:
        html = '''
        <div class="whatever-generated-class">
          <a href="/games/mad-hit-vegas/">
            <img src="data:image/svg+xml,x" data-src="/img/mad.jpg">
          </a>
          <h3><a href="/games/mad-hit-vegas/">Mad Hit Vegas</a></h3>
          <a href="/game_theme/vegas/">Vegas</a>
        </div>
        <a href="/game_theme/not-a-game/">Not game</a>
        '''
        records = parse_catalog_html(html, "https://rubyplay.com/games/")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.game.slug, "mad-hit-vegas")
        self.assertEqual(record.game.name, "Mad Hit Vegas")
        self.assertEqual(
            record.game.thumbnail_url,
            "https://rubyplay.com/img/mad.jpg",
        )

    def test_updated_query_is_authoritative_metadata(self) -> None:
        payload = {
            "updated_query": {
                "element_id": "dynamic-loop",
                "count": 191,
                "max_num_pages": 6,
                "start": 161,
                "end": 191,
            }
        }
        meta = updated_query_meta(payload)
        self.assertEqual(
            meta,
            {
                "count": 191,
                "max_pages": 6,
                "start": 161,
                "end": 191,
            },
        )
        self.assertEqual(updated_query_element_id(payload), "dynamic-loop")


if __name__ == "__main__":
    unittest.main()
