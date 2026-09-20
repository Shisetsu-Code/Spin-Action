from pathlib import Path
import json


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected patch context not found: {path}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "tester_spin/providers/bgaming/execution.py",
    '''                                allow_observed_effective_bet=(
                                    mode_id == "SPIN"
                                    and not isinstance(purchase, dict)
                                    and observed_base_bet_multiplier == 1.0
                                ),''',
    '''                                allow_observed_effective_bet=(
                                    mode_id == "SPIN"
                                    and not isinstance(purchase, dict)
                                    and observed_base_bet_multiplier == 1.0
                                    and active_profile is not None
                                    and bool(active_profile.spin_options)
                                ),''',
)

replace_once(
    "tester_spin/providers/bgaming/execution.py",
    '''                            if (
                                isinstance(actual_base_bet, (int, float))
                                and actual_base_bet > 0
                                and float(default_bet) > 0
                            ):
                                learned_ratio = float(actual_base_bet) / float(default_bet)
                                if abs(learned_ratio - 1.0) > 1e-9:''',
    '''                            if (
                                isinstance(actual_base_bet, (int, float))
                                and actual_base_bet > 0
                                and isinstance(expected_outcome_bet, (int, float))
                                and float(expected_outcome_bet) > 0
                            ):
                                # Learn only the residual multiplier not already
                                # explained by a provider/client selector contract.
                                # This prevents double-applying explicit effective-bet
                                # tables while still allowing a structurally proven
                                # mode/rows option to establish a server wager scale.
                                learned_ratio = (
                                    float(actual_base_bet) / float(expected_outcome_bet)
                                )
                                if abs(learned_ratio - 1.0) > 1e-9:''',
)

replace_once(
    "tester_spin/providers/bgaming/hyperhive_wire.py",
    '''    if not profile.custom_req:
        return
    mode["custom_req_profile"] = profile.custom_profile''',
    '''    if not profile.custom_req:
        return
    # A pre-existing serializer profile is stronger than a later generic
    # formatted-custom_req observation. Never replace proven wire shape without
    # evidence that the existing provider-derived profile is invalid.
    if str(mode.get("custom_req_profile") or ""):
        return
    mode["custom_req_profile"] = profile.custom_profile''',
)

replace_once(
    "tests/test_bgaming_hyperhive_wire.py",
    '''    def test_observed_serializer_replaces_legacy_pz_profile(self) -> None:
        mode = {
            "id": "SPIN",
            "kind": "SPIN",
            "executable": True,
            "custom_req_profile": "pz-per-line",
            "discovery_state": "BASE_CONTRACT",
            "source": "engine-heuristic",
        }
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_stake_on_spin=True,
            custom_literals={"isNormalBuy": False},
        )
        _apply_observed_custom_profile_to_base_mode(mode, profile)
        self.assertEqual(mode["custom_req_profile"], "observed-formatted-stake")
        self.assertEqual(mode["custom_req_literal_keys"], ["isNormalBuy"])
        self.assertEqual(mode["discovery_state"], "OBSERVED_ENGINE_CONTRACT")
        self.assertEqual(mode["source"], "live-inner-client+engine-contract")
''',
    '''    def test_observed_serializer_does_not_replace_legacy_pz_profile(self) -> None:
        mode = {
            "id": "SPIN",
            "kind": "SPIN",
            "executable": True,
            "custom_req_profile": "pz-per-line",
            "discovery_state": "BASE_CONTRACT",
            "source": "engine-heuristic",
        }
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_stake_on_spin=True,
            custom_literals={"isNormalBuy": False},
        )
        _apply_observed_custom_profile_to_base_mode(mode, profile)
        self.assertEqual(mode["custom_req_profile"], "pz-per-line")
        self.assertNotIn("custom_req_literal_keys", mode)
        self.assertEqual(mode["discovery_state"], "BASE_CONTRACT")
        self.assertEqual(mode["source"], "engine-heuristic")
''',
)

cfg = Path(".github/provider-validation.json")
data = json.loads(cfg.read_text(encoding="utf-8"))
data["run_sequence"] = int(data.get("run_sequence") or 0) + 1
data["slugs"] = (
    "dice-million,fortune-bells,burning-chilli-x,fishing-club,"
    "fishing-club-2,krakens-hunger,royal-fruits-multilines,wild-tiger"
)
cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
