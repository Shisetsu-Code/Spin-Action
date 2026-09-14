from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.catalog import parse_catalog_html


class RubyPlayCatalogNameTests(unittest.TestCase):
    def test_card_heading_wins_over_generic_read_more_link(self) -> None:
        html = '''
        <div class="game-card">
          <a href="/games/immortal-ways-pinata/">
            <img src="/img/pinata.jpg" alt="">
          </a>
          <h3>Immortal Ways® Piñata</h3>
          <div class="theme">Mexican/Colombian</div>
          <a href="/games/immortal-ways-pinata/">Read More</a>
        </div>
        '''
        records = parse_catalog_html(html, "https://rubyplay.com/games/")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].game.slug, "immortal-ways-pinata")
        self.assertEqual(records[0].game.name, "Immortal Ways® Piñata")


if __name__ == "__main__":
    unittest.main()
