from __future__ import annotations

import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.farm_structure import attach_execution_structure, select_domains
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)
from tester_spin.providers.rubyplay.choice_domains import rubyplay_choice_domain_is_proven
from tester_spin.providers.rubyplay.choice_exhaustive import RubyPlayProvider as _RubyPlayProvider
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions
from tester_spin.providers.rubyplay.purchase_coverage import build_rubyplay_purchase_coverage


_SCOPE_LOCAL = threading.local()

_SPEC = ProviderFarmSpec(
    provider="rubyplay",
    protocol_family="rubyplay-gameserver",
    bootstrap_strategy="rubyplay-public-demo-http",
    transport="http-json",
    terminal_contract={
        "type": "provider",
        "name": "rubyplay-next-action",
        "terminal_next_action": "spin",
    },
    identifier_metadata_keys=("identifier",),
    stable_metadata_keys=(
        "identifier",
        "runtime_transport",
        "state_authority",
        "state_carrier",
        "bet_profile",
        "client_profile",
    ),
    mode_option_keys=(
        "bet_policy",
        "allowed_bets",
        "default_bet",
        "wager",
        "effective_stake",
        "buy_feature_type",
        "pricing_basis",
        "feature_multiplier",
        "default_price",
        "observed_indices",
        "boundary_index",
        "boundary_confirmations",
        "rejection_span",
        "prompt_retry_budget",
        "domain_authority",
    ),
    runtime_outputs=("launcher_parameters", "gameserver_endpoint", "action_number"),
    protocol_static={
        "state_authority": "response.data.next_action",
        "base_action": "spin",
        "known_continuation_actions": ["freespin", "respin", "minispin", "select", "pick"],
    },
)


def _gate_rubyplay_choice_domains(
    contract: dict,
    result: GameTestResult,
) -> dict:
    unproven: set[str] = set()
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("kind") or "").upper() != "INDEXED_CHOICE":
            continue
        if rubyplay_choice_domain_is_proven(mode):
            continue
        mode_id = str(mode.get("id") or "").strip()
        if mode_id:
            unproven.add(mode_id)

    if not unproven:
        return contract

    unresolved = contract.get("unresolved")
    if not isinstance(unresolved, list):
        unresolved = []
        contract["unresolved"] = unresolved
    for mode_id in sorted(unproven):
        reason = f"RUBYPLAY_CHOICE_DOMAIN_UNPROVEN:{mode_id}"
        if reason not in unresolved:
            unresolved.append(reason)

    modes = contract.get("modes")
    if isinstance(modes, list):
        for mode in modes:
            if not isinstance(mode, dict):
                continue
            if str(mode.get("id") or "") not in unproven:
                continue
            mode["evidence"] = "NO_VALIDADO"
            mode["required"] = True

    contract["ready"] = False
    return contract


def _gate_rubyplay_execution_choices(contract: dict) -> dict:
    unresolved = contract.get("unresolved")
    blocked: set[str] = set()
    if isinstance(unresolved, list):
        prefix = "RUBYPLAY_CHOICE_DOMAIN_UNPROVEN:"
        blocked = {
            str(value)[len(prefix):]
            for value in unresolved
            if str(value).startswith(prefix)
        }
    structure = contract.get("execution_structure")
    choices = structure.get("choices") if isinstance(structure, dict) else None
    if isinstance(choices, list):
        for choice in choices:
            if (
                isinstance(choice, dict)
                and str(choice.get("mode_id") or "") in blocked
            ):
                choice["coverage_complete"] = False
    return contract


class _RateLimitedRubyPlaySession:
    """Transparent session proxy using the provider-wide request budget."""

    def __init__(self, provider: "RubyPlayProvider", inner) -> None:
        self._provider = provider
        self._inner = inner
        self.headers = inner.headers

    def _reserve(self) -> None:
        if not self._provider.acquire_provider_request_slot():
            raise InterruptedError(
                "RubyPlay request cancelled while waiting for provider rate-limit slot"
            )

    def get(self, *args, **kwargs):
        self._reserve()
        return self._inner.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        self._reserve()
        return self._inner.post(*args, **kwargs)

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RubyPlayProvider(_RubyPlayProvider):
    """Active RubyPlay provider with post-discovery farm export hooks."""

    # Keep one launcher/runtime at a time until RubyPlay multi-session behavior is
    # explicitly validated. This avoids shared launcher/session state collisions.
    max_test_concurrency = 1

    def _new_session(self):
        return _RateLimitedRubyPlaySession(self, super()._new_session())

    def rubyplay_execution_scope(self) -> str:
        return str(getattr(_SCOPE_LOCAL, "value", "ALL") or "ALL")

    def _run_scoped(
        self,
        scope: str,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        previous = getattr(_SCOPE_LOCAL, "value", None)
        _SCOPE_LOCAL.value = str(scope or "ALL").upper()
        try:
            return self.test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            if previous is None:
                try:
                    delattr(_SCOPE_LOCAL, "value")
                except AttributeError:
                    pass
            else:
                _SCOPE_LOCAL.value = previous

    def test_natural_spins(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        return self._run_scoped(
            "NATURAL_ONLY",
            game,
            spins=max(1, int(spins)),
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def test_purchase_paths(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        return self._run_scoped(
            "PURCHASE_ONLY",
            game,
            spins=1,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_feature_sessions(self, result: GameTestResult) -> dict:
        return build_rubyplay_feature_sessions(result)

    def build_purchase_coverage(self, game: Game, result: GameTestResult) -> dict:
        del game
        return build_rubyplay_purchase_coverage(result)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        contract = _gate_rubyplay_choice_domains(contract, result)
        protocol = contract.get("protocol")
        stable = protocol.get("stable_metadata") if isinstance(protocol, dict) else {}
        domains = select_domains(stable, ("bet_profile", "client_profile"))
        contract = attach_execution_structure(contract, provider_domains=domains)
        return _gate_rubyplay_execution_choices(contract)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        errors = list(validate_result_farm_contract(contract, _SPEC))
        modes = contract.get("modes")
        if isinstance(modes, list):
            for mode in modes:
                if not isinstance(mode, dict):
                    continue
                if str(mode.get("kind") or "").upper() != "INDEXED_CHOICE":
                    continue
                options = mode.get("options")
                candidate = {"kind": "INDEXED_CHOICE"}
                if isinstance(options, dict):
                    candidate.update(options)
                if rubyplay_choice_domain_is_proven(candidate):
                    continue
                mode_id = str(mode.get("id") or "UNKNOWN")
                errors.append(f"RUBYPLAY_CHOICE_DOMAIN_UNPROVEN:{mode_id}")
        return list(dict.fromkeys(errors))


RubyPlayProvider.__module__ = "tester_spin.providers.rubyplay.exhaustive"

__all__ = ["RubyPlayProvider", "_RateLimitedRubyPlaySession"]
