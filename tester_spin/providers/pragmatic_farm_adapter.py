from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.farm_structure import attach_execution_structure
from tester_spin.providers.pragmatic import PragmaticProvider as _BasePragmaticProvider
from tester_spin.providers.pragmatic_action_inventory import annotate_pragmatic_action_inventory
from tester_spin.providers.pragmatic_exhaustive import PragmaticProvider as _PragmaticProvider
from tester_spin.providers.pragmatic_purchase_coverage import build_pragmatic_purchase_coverage
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)


_SPEC = ProviderFarmSpec(
    provider="pragmatic",
    protocol_family="pragmatic-game-service",
    bootstrap_strategy="pragmatic-public-demo-http",
    transport="http-form-urlencoded",
    terminal_contract={
        "type": "provider",
        "name": "pragmatic-na-terminal",
        "terminal_states": ["", "s"],
        "requires_feature_inactive": True,
    },
    identifier_metadata_keys=("provider_internal_id",),
    stable_metadata_keys=("provider_internal_id",),
    mode_option_keys=(
        "provider_bl",
        "provider_pur",
        "ordinal",
        "price_x_base",
        "paid_cost",
        "price_known",
        "source_field",
        "source_value",
    ),
    runtime_outputs=("mgckey", "game_service_endpoint", "index", "counter"),
    protocol_static={
        "entry_action": "doSpin",
        "known_continuation_actions": [
            "doBonus",
            "doCollectBonus",
            "doCollect",
            "doSpin",
            "doFSOption",
            "doMysteryScatter",
        ],
    },
)


class PragmaticProvider(_PragmaticProvider):
    """Active Pragmatic provider with post-discovery farm export hooks."""

    # Pragmatic execution creates isolated runtime sessions per game. Keep the
    # initial purchase campaign cap at four simultaneous games; all protocol
    # requests still share one paced provider-wide limiter.
    max_test_concurrency = 4

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def _http_bootstrap(
        self,
        source_url: str,
        symbol: str,
        cver: str | None,
        base_bet: float,
        timeout_s: float,
    ):
        # The direct bootstrap performs doInit plus one calibration doSpin.
        # Reserve both provider-protocol slots up front so a campaign cannot
        # create a bootstrap burst even though the base implementation owns the
        # requests.Session internally.
        for _ in range(2):
            if not self.acquire_provider_request_slot():
                raise InterruptedError("Pragmatic bootstrap cancelled while waiting for rate-limit slot")
        return _BasePragmaticProvider._http_bootstrap(
            self,
            source_url,
            symbol,
            cver,
            base_bet,
            timeout_s,
        )

    def _post_and_store(
        self,
        bootstrap,
        fields,
        root,
        *,
        step: int,
        label: str,
        timeout_s: float,
    ):
        # Every state-changing gameService POST, including long feature
        # continuations, consumes one shared provider slot.
        if not self.acquire_provider_request_slot():
            raise InterruptedError("Pragmatic state request cancelled while waiting for rate-limit slot")
        return _BasePragmaticProvider._post_and_store(
            self,
            bootstrap,
            fields,
            root,
            step=step,
            label=label,
            timeout_s=timeout_s,
        )

    def test_purchase_paths(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event,
        progress,
    ) -> GameTestResult:
        """Run one exhaustive Pragmatic iteration for purchase coverage.

        A root ``pur`` request is not enough to close a purchased feature. If the
        purchased round exposes FSO selectors, the existing exhaustive wrapper
        must replay every provider-announced sibling before purchase coverage can
        be promoted.
        """
        return _PragmaticProvider.test_game(
            self,
            game,
            spins=1,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def finalize_test_result(self, result: GameTestResult, *, progress) -> GameTestResult:
        result = super().finalize_test_result(result, progress=progress)
        annotate_pragmatic_action_inventory(result)
        inventory = (
            result.structural_map.get("action_inventory", {})
            if isinstance(result.structural_map, dict)
            else {}
        )
        state = str(inventory.get("state") or "UNKNOWN")
        reason = str(inventory.get("reason") or "")
        progress(f"Pragmatic inventario de acciones: {state}. {reason}".strip())
        return result

    def build_purchase_coverage(self, game: Game, result: GameTestResult) -> dict:
        del game
        return build_pragmatic_purchase_coverage(result)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        return attach_execution_structure(contract)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


PragmaticProvider.__module__ = "tester_spin.providers.pragmatic_hybrid"

__all__ = ["PragmaticProvider"]
