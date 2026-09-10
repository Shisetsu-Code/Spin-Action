from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tester_spin.providers.bgaming.contracts import SAFE_CONTINUATION_COMMANDS
from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    discover_api_v2_wire_profile,
    discover_purchase_modes,
    _provider_script_url,
    is_line_bet_init,
    is_switchable_container_init,
    line_bet_count,
)


PROFILE_SCHEMA = "tester-spin/bgaming-profile/v1"

API_V2 = "api-v2"
LEGACY_LINES = "legacy-lines"
HYPERHIVE = "hyperhive-jsonrpc"
SWITCHABLE = "switchable-container"
UNKNOWN = "unknown"

@dataclass(slots=True)
class RuntimeClassification:
    family: str
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


@dataclass(slots=True)
class BGamingProfile:
    capability_version: int = 2
    family: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    spin_options: dict[str, Any] = field(default_factory=dict)
    command_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_extra_data: dict[str, Any] = field(default_factory=dict)
    spin_option_choices: dict[str, list[Any]] = field(default_factory=dict)
    effective_bet_selector: str = ""
    effective_bet_multipliers: dict[str, float] = field(default_factory=dict)
    dynamic_purchased_feature: bool = False
    purchase_feature_level_supported: bool = False
    purchase_features: list[str] = field(default_factory=list)
    rows_required: bool = False
    line_count: int = 0
    variable_layout: bool = False
    allowed_continuations: list[str] = field(default_factory=list)
    source: str = ""
    bundle_sha256: str = ""
    discovery_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    validated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA,
            "capability_version": self.capability_version,
            "family": self.family,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "spin_options": dict(self.spin_options),
            "command_options": {
                str(command): dict(options)
                for command, options in self.command_options.items()
                if isinstance(options, dict)
            },
            "request_extra_data": dict(self.request_extra_data),
            "spin_option_choices": {
                str(name): list(values)
                for name, values in self.spin_option_choices.items()
                if isinstance(values, list)
            },
            "effective_bet_selector": self.effective_bet_selector,
            "effective_bet_multipliers": dict(self.effective_bet_multipliers),
            "dynamic_purchased_feature": self.dynamic_purchased_feature,
            "purchase_feature_level_supported": self.purchase_feature_level_supported,
            "purchase_features": list(self.purchase_features),
            "rows_required": self.rows_required,
            "line_count": self.line_count,
            "variable_layout": self.variable_layout,
            "allowed_continuations": list(self.allowed_continuations),
            "source": self.source,
            "bundle_sha256": self.bundle_sha256,
            "discovery_diagnostics": list(self.discovery_diagnostics),
            "validated": self.validated,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BGamingProfile | None":
        if not isinstance(value, dict):
            return None
        if value.get("schema") not in {None, PROFILE_SCHEMA}:
            return None
        capability_version = int(value.get("capability_version") or 0)
        family = str(value.get("family") or UNKNOWN)
        options = value.get("spin_options")
        command_options = value.get("command_options")
        request_extra_data = value.get("request_extra_data")
        spin_option_choices = value.get("spin_option_choices")
        effective_bet_multipliers = value.get("effective_bet_multipliers")
        purchase_features = value.get("purchase_features")
        continuations = value.get("allowed_continuations")
        evidence = value.get("evidence")
        diagnostics = value.get("discovery_diagnostics")
        return cls(
            capability_version=capability_version,
            family=family,
            confidence=float(value.get("confidence") or 0.0),
            evidence=[str(x) for x in evidence] if isinstance(evidence, list) else [],
            spin_options=dict(options) if isinstance(options, dict) else {},
            command_options={
                str(command): dict(command_value)
                for command, command_value in command_options.items()
                if isinstance(command_value, dict)
            } if isinstance(command_options, dict) else {},
            request_extra_data=dict(request_extra_data)
            if isinstance(request_extra_data, dict)
            else {},
            spin_option_choices={
                str(name): list(values)
                for name, values in spin_option_choices.items()
                if isinstance(values, list)
            } if isinstance(spin_option_choices, dict) else {},
            effective_bet_selector=str(value.get("effective_bet_selector") or ""),
            effective_bet_multipliers={
                str(key): float(multiplier)
                for key, multiplier in effective_bet_multipliers.items()
                if isinstance(multiplier, (int, float))
            } if isinstance(effective_bet_multipliers, dict) else {},
            dynamic_purchased_feature=bool(value.get("dynamic_purchased_feature")),
            purchase_feature_level_supported=bool(
                value.get("purchase_feature_level_supported")
            ),
            purchase_features=[
                str(item) for item in purchase_features if str(item)
            ] if isinstance(purchase_features, list) else [],
            rows_required=bool(value.get("rows_required")),
            line_count=max(0, int(value.get("line_count") or 0)),
            variable_layout=bool(value.get("variable_layout")),
            allowed_continuations=[
                str(x) for x in continuations
                if str(x) in SAFE_CONTINUATION_COMMANDS
            ] if isinstance(continuations, list) else [],
            source=str(value.get("source") or ""),
            bundle_sha256=str(value.get("bundle_sha256") or ""),
            discovery_diagnostics=[
                dict(item) for item in diagnostics if isinstance(item, dict)
            ] if isinstance(diagnostics, list) else [],
            validated=bool(value.get("validated")),
        )


def classify_runtime(
    runtime: BGamingRuntime,
    init_data: dict[str, Any] | None = None,
) -> RuntimeClassification:
    evidence: list[str] = []
    launch_path = urlparse(runtime.launch_url).path.rstrip("/").casefold()

    if launch_path.endswith("/hyperhive"):
        return RuntimeClassification(
            family=HYPERHIVE,
            confidence=1.0,
            evidence=["launch_path:/hyperhive"],
        )

    data = init_data if isinstance(init_data, dict) else {}
    if data:
        if is_line_bet_init(data):
            return RuntimeClassification(
                family=LEGACY_LINES,
                confidence=1.0,
                evidence=["init.options.line_bets", "init.options.lines"],
            )

        if is_switchable_container_init(data):
            evidence.extend(["init.top_level.wallet", "init.top_level.game", "init.no_options"])
            if str(runtime.options.get("lobby_launch_url") or "").strip():
                evidence.append("bootstrap.lobby_launch_url")
                return RuntimeClassification(
                    family=SWITCHABLE,
                    confidence=1.0,
                    evidence=evidence,
                )
            return RuntimeClassification(
                family=UNKNOWN,
                confidence=0.45,
                evidence=evidence + ["missing:lobby_launch_url"],
            )

        options = data.get("options")
        api_version = str(data.get("api_version") or "")
        if isinstance(options, dict):
            evidence.append("init.options")
            if api_version == "2":
                evidence.append("init.api_version=2")
                return RuntimeClassification(
                    family=API_V2,
                    confidence=1.0,
                    evidence=evidence,
                )
            # Several BGaming API-v2-like games omit/rewrite api_version, but an
            # options object plus a usable bootstrap API is enough to classify the
            # transport family while keeping confidence below a strict match.
            if runtime.api_url:
                evidence.append("bootstrap.api_url")
                return RuntimeClassification(
                    family=API_V2,
                    confidence=0.80,
                    evidence=evidence,
                )

    return RuntimeClassification(
        family=UNKNOWN,
        confidence=0.0,
        evidence=evidence or ["no-known-runtime-signature"],
    )


def infer_variable_layout(init_data: dict[str, Any]) -> bool:
    options = init_data.get("options")
    if not isinstance(options, dict):
        return False
    layout = options.get("layout")
    if not isinstance(layout, dict):
        return False

    # Prefer explicit structural flags. No game names or identifiers participate.
    for key in (
        "variable_rows",
        "dynamic_rows",
        "variable_layout",
        "dynamic_layout",
        "megaways",
        "ways",
    ):
        value = layout.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and value > 0:
            return True

    rows = layout.get("rows")
    return isinstance(rows, (int, float)) and rows >= 7


def discover_profile(
    runtime: BGamingRuntime,
    init_data: dict[str, Any],
    *,
    timeout_s: float,
    persisted: BGamingProfile | None = None,
    wire_profile: dict[str, Any] | None = None,
) -> BGamingProfile:
    classification = classify_runtime(runtime, init_data)
    profile = BGamingProfile(
        family=classification.family,
        confidence=classification.confidence,
        evidence=list(classification.evidence),
        request_extra_data=dict(runtime.request_extra_data),
        variable_layout=infer_variable_layout(init_data),
    )

    if classification.family == LEGACY_LINES:
        profile.line_count = line_bet_count(init_data)
        profile.allowed_continuations = []
        profile.source = "init"
        return profile

    if classification.family != API_V2:
        profile.source = "bootstrap+init"
        return profile

    if (
        persisted is not None
        and persisted.family == API_V2
        and persisted.validated
        and persisted.capability_version >= 2
    ):
        advertised_purchases = discover_purchase_modes(init_data)
        persisted_source = str(persisted.source or "")
        parsed_source = urlparse(persisted_source)
        third_party_source = bool(
            parsed_source.scheme in {"http", "https"}
            and not _provider_script_url(runtime, persisted_source)
        )
        purchase_contract_missing = bool(
            advertised_purchases and not persisted.purchase_features
        )

        profile.spin_options.update(persisted.spin_options)
        profile.command_options = {
            command: dict(options)
            for command, options in persisted.command_options.items()
        }
        if not profile.request_extra_data:
            profile.request_extra_data = dict(persisted.request_extra_data)
        profile.spin_option_choices = {
            name: list(values)
            for name, values in persisted.spin_option_choices.items()
        }
        profile.effective_bet_selector = persisted.effective_bet_selector
        profile.effective_bet_multipliers = dict(
            persisted.effective_bet_multipliers
        )
        profile.dynamic_purchased_feature = persisted.dynamic_purchased_feature
        profile.purchase_feature_level_supported = (
            persisted.purchase_feature_level_supported
        )
        profile.purchase_features = list(persisted.purchase_features)
        profile.rows_required = persisted.rows_required
        profile.allowed_continuations = [
            item for item in persisted.allowed_continuations
            if item in SAFE_CONTINUATION_COMMANDS
        ]
        profile.bundle_sha256 = persisted.bundle_sha256
        profile.discovery_diagnostics = list(persisted.discovery_diagnostics)
        profile.validated = True
        profile.evidence.append("persisted.validated")

        if not third_party_source and not purchase_contract_missing:
            profile.source = "persisted-validated-profile"
            return profile

        profile.validated = False
        if third_party_source:
            profile.evidence.append("refresh:third-party-source")
        if purchase_contract_missing:
            profile.evidence.append("refresh:purchase-contract-missing")

    wire = (
        dict(wire_profile)
        if isinstance(wire_profile, dict)
        else discover_api_v2_wire_profile(runtime, timeout_s=timeout_s)
    )
    request_extra_data = wire.get("request_extra_data")
    if isinstance(request_extra_data, dict):
        profile.request_extra_data.update(request_extra_data)

    options = wire.get("spin_options")
    if isinstance(options, dict):
        profile.spin_options.update(options)

    option_choices = wire.get("spin_option_choices")
    if isinstance(option_choices, dict):
        profile.spin_option_choices = {
            str(name): list(values)
            for name, values in option_choices.items()
            if isinstance(values, list) and values
        }

    effective_multipliers = wire.get("effective_bet_multipliers")
    if isinstance(effective_multipliers, dict):
        profile.effective_bet_multipliers = {
            str(key): float(multiplier)
            for key, multiplier in effective_multipliers.items()
            if isinstance(multiplier, (int, float)) and multiplier > 0
        }

    profile.dynamic_purchased_feature = bool(
        wire.get("dynamic_purchased_feature")
    )
    profile.purchase_feature_level_supported = bool(
        wire.get("purchase_feature_level_supported")
    )

    required_fields = wire.get("required_option_fields")
    init_options = init_data.get("options")
    init_layout = (
        init_options.get("layout")
        if isinstance(init_options, dict)
        else None
    )
    if isinstance(required_fields, list) and isinstance(init_options, dict):
        for raw_field in required_fields:
            field = str(raw_field or "").strip()
            if not field or field in profile.spin_options:
                continue
            value = init_options.get(field)
            if value is None and isinstance(init_layout, dict):
                value = init_layout.get(field)
            if isinstance(value, (str, int, float, bool)):
                profile.spin_options[field] = value
                profile.evidence.append(
                    f"client.additionalSpinOptions.{field}"
                )
                continue

            choices = profile.spin_option_choices.get(field)
            if isinstance(choices, list) and choices:
                # Choose one value deterministically from client-proven choices.
                # Tester-Spin is validating protocol reachability, not mimicking
                # a user's persisted UI preference.
                profile.spin_options[field] = choices[0]
                profile.evidence.append(
                    f"client.additionalSpinOptions.{field}:choice"
                )

    selector_candidates: list[str] = []
    multiplier_keys = set(profile.effective_bet_multipliers)
    if multiplier_keys:
        for field, choices in profile.spin_option_choices.items():
            choice_keys = {str(value) for value in choices}
            if len(multiplier_keys & choice_keys) >= 2:
                selector_candidates.append(field)
    if len(selector_candidates) == 1:
        profile.effective_bet_selector = selector_candidates[0]
        profile.evidence.append(
            f"client.effective_bet_selector.{profile.effective_bet_selector}"
        )

    purchase_features = wire.get("purchase_features")
    discovered_purchase_features = {
        str(item) for item in purchase_features if str(item)
    } if isinstance(purchase_features, list) else set()
    if profile.dynamic_purchased_feature:
        discovered_purchase_features.update(
            str(mode.get("name") or "")
            for mode in discover_purchase_modes(init_data)
            if str(mode.get("name") or "")
        )
    profile.purchase_features = sorted(discovered_purchase_features)
    profile.source = str(wire.get("source") or "init")
    profile.bundle_sha256 = str(wire.get("bundle_sha256") or "")
    diagnostics = wire.get("diagnostics")
    profile.discovery_diagnostics = [
        dict(item) for item in diagnostics if isinstance(item, dict)
    ] if isinstance(diagnostics, list) else []
    profile.allowed_continuations = sorted(SAFE_CONTINUATION_COMMANDS)
    return profile


def load_profile(game_json: Path) -> BGamingProfile | None:
    if not game_json.is_file():
        return None
    try:
        raw = json.loads(game_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return BGamingProfile.from_dict(raw.get("provider_protocol"))


def save_profile(game_json: Path, profile: BGamingProfile) -> None:
    current: dict[str, Any] = {}
    if game_json.is_file():
        try:
            loaded = json.loads(game_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:
            current = {}
    current["provider_protocol"] = profile.to_dict()
    game_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = game_json.with_suffix(game_json.suffix + ".tmp")
    tmp.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(game_json)


def profile_fingerprint(profile: BGamingProfile) -> str:
    raw = json.dumps(
        profile.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
