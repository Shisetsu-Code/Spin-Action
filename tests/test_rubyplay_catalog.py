from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.catalog import (
    load_query_payload,
    parse_bricks_catalog_state,
    parse_catalog_html,
    query_loop_html,
    updated_query_meta,
)


class RubyPlayCatalogTests(unittest.TestCase):
    def test_bricks_query_is_found_by_post_type_not_element_id(self) -> None:
        html = r'''
        <script>
        bricksData = {
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en"
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
        payload = load_query_payload(state, 2)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["queryElementId"], "generated-id-123")
        self.assertIn('"post_type":["games"]', payload["queryVars"])

    def test_multiple_game_queries_select_main_catalog_by_structure(self) -> None:
        side_trails = []
        side_loops = []
        for index in range(7):
            query_id = f"side{index}"
            side_trails.append(
                f'''<div class="brx-query-trail"
                    data-query-element-id="{query_id}"
                    data-query-vars='{{"post_type":["games"],"posts_per_page":4,"paged":1}}'
                    data-page="1" data-max-pages="1"
                    data-start="1" data-end="4"></div>'''
            )
            side_loops.append(
                f'''<!--brx-loop-start-{query_id}-->
                <div class="brxe-{query_id}"><a href="/games/side-{index}/">Side {index}</a></div>
                <!--brx-loop-end-{query_id}-->'''
            )

        html = f'''
        <script>
        bricksData = {{
          restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
          nonce: "query-nonce",
          wpRestNonce: "rest-nonce",
          postId: "10149",
          language: "en"
        }};
        </script>
        {''.join(side_trails)}
        <div class="brx-query-trail"
             data-query-element-id="main-games"
             data-query-vars='{{"post_type":["games"],"posts_per_page":32,"paged":1}}'
             data-page="1" data-max-pages="6"
             data-start="1" data-end="32"></div>
        {''.join(side_loops)}
        <!--brx-loop-start-main-games-->
          <div class="brxe-main-games"><a href="/games/main-a/">Main A</a></div>
          <div class="brxe-main-games"><a href="/games/main-b/">Main B</a></div>
        <!--brx-loop-end-main-games-->
        '''

        state = parse_bricks_catalog_state(html, "https://rubyplay.com/games/")
        self.assertEqual(state.candidate_count, 8)
        self.assertEqual(state.query_element_id, "main-games")
        self.assertEqual(state.max_pages, 6)

        fragment = query_loop_html(html, state.query_element_id)
        records = parse_catalog_html(fragment, "https://rubyplay.com/games/")
        self.assertEqual([item.game.slug for item in records], ["main-a", "main-b"])

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
        meta = updated_query_meta(
            {
                "updated_query": {
                    "count": 191,
                    "max_num_pages": 6,
                    "start": 161,
                    "end": 191,
                }
            }
        )
        self.assertEqual(
            meta,
            {
                "count": 191,
                "max_pages": 6,
                "start": 161,
                "end": 191,
            },
        )


if __name__ == "__main__":
    unittest.main()
