from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.money_display import humanize_bgaming_progress


class BGamingMoneyDisplayTests(unittest.TestCase):
    def test_init_balance_is_displayed_as_fun_not_raw_minor_units(self) -> None:
        message = (
            "[Game] INIT OK: identifier=Game, bet=25 (default_bet), "
            "balance_total=100000, compras=5"
        )
        rendered = humanize_bgaming_progress(message)
        self.assertIn("bet=0.25 FUN (raw=25)", rendered)
        self.assertIn("balance_total=1000.00 FUN (raw=100000)", rendered)

    def test_spin_amounts_are_scaled_but_round_ids_are_not(self) -> None:
        message = (
            "[Game] SPIN 1/1: OK, round=17255245674, action=17255245674_1, "
            "bet=25, debit=1000, win=325, balance=99325"
        )
        rendered = humanize_bgaming_progress(message)
        self.assertIn("round=17255245674", rendered)
        self.assertIn("action=17255245674_1", rendered)
        self.assertIn("bet=0.25 FUN (raw=25)", rendered)
        self.assertIn("debit=10.00 FUN (raw=1000)", rendered)
        self.assertIn("win=3.25 FUN (raw=325)", rendered)
        self.assertIn("balance=993.25 FUN (raw=99325)", rendered)

    def test_line_bet_and_inferred_win_are_humanized(self) -> None:
        rendered = humanize_bgaming_progress(
            "line_bet=1, debit=15, win_inferido=33.0, balance=100018"
        )
        self.assertIn("line_bet=0.01 FUN (raw=1)", rendered)
        self.assertIn("debit=0.15 FUN (raw=15)", rendered)
        self.assertIn("win_inferido=0.33 FUN (raw=33.0)", rendered)
        self.assertIn("balance=1000.18 FUN (raw=100018)", rendered)

    def test_non_numeric_and_multipliers_are_untouched(self) -> None:
        message = "win=—, balance=—, costo=x1.6, round=123456789"
        self.assertEqual(humanize_bgaming_progress(message), message)

    def test_already_humanized_value_is_not_reformatted(self) -> None:
        message = "balance=1000.00 FUN (raw=100000)"
        self.assertEqual(humanize_bgaming_progress(message), message)


if __name__ == "__main__":
    unittest.main()
