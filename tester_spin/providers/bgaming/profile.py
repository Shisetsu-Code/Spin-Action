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
    family: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    spin_options: dict[str, Any] = field(default_factory=dict)
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
            "family": self.family,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "spin_options": dict(self.spin_options),
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
        family = str(value.get("family") or UNKNOWN)
        options = value.get("spin_options")
        continuations = value.get("allowed_continuations")
        evidence = value.get("evidence")
        diagnostics = value.get("discovery_diagnostics")
        return cls(
            family=family,
            confidence=float(value.get("confidence") or 0.0),
            evidence=[str(x) for x in evidence] if isinstance(evidence, list) else [],
            spin_options=dict(options) if isinstance(options, dict) else {},
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
) -> BGamingProfile:
    classification = classify_runtime(runtime, init_data)
    profile = BGamingProfile(
        family=classification.family,
        confidence=classification.confidence,
        evidence=list(classification.evidence),
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
    ):
        profile.spin_options.update(persisted.spin_options)
        profile.rows_required = persisted.rows_required
        profile.allowed_continuations = [
            item for item in persisted.allowed_continuations
            if item in SAFE_CONTINUATION_COMMANDS
        ]
        profile.source = "persisted-validated-profile"
        profile.bundle_sha256 = persisted.bundle_sha256
        profile.discovery_diagnostics = list(persisted.discovery_diagnostics)
        profile.validated = True
        profile.evidence.append("persisted.validated")
        return profile

    wire = discover_api_v2_wire_profile(runtime, timeout_s=timeout_s)
    options = wire.get("spin_options")
    if isinstance(options, dict):
        profile.spin_options.update(options)
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
