"""BGaming evidence adapter. Observing never authorizes or changes dispatch."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from tester_spin.structure import Choice, Observation, State, StructuralMap, atomic_write, persist_map
from tester_spin.structure.normalization import identity, sanitize

_CURRENT: ContextVar["Capture | None"] = ContextVar("bgaming_structure", default=None)


def _state(data: dict) -> State | None:
    flow = data.get("flow")
    if not isinstance(flow, dict) or not isinstance(flow.get("state"), str):
        return None
    actions = flow.get("available_actions")
    actions = tuple(str(a) for a in actions) if isinstance(actions, list) else ()
    # The existing BGaming validator treats closed + a new spin as a finished round.
    terminal = flow["state"] == "closed" if "spin" in actions else None
    return State(flow["state"], actions, terminal)


@dataclass
class Capture:
    graph: StructuralMap
    previous: State | None = None
    context: dict = field(default_factory=dict)
    prefix: list[str] = field(default_factory=list)
    fingerprints: set[str] = field(default_factory=set)
    diagnostics: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.previous = None
        self.context = {}
        self.prefix = []

    def wire(self, request: dict, response: dict | None, failed: bool) -> None:
        command = str(request.get("command") or "unknown")
        if command == "init":
            self.reset()
        elif command == "spin":
            self.context = {}
            self.prefix = self.prefix[:1]
        before = self.previous or State("<unobserved>")
        after = _state(response) if isinstance(response, dict) else None
        valid = after is not None and not failed and not response.get("errors") and not response.get("error")
        options = request.get("options") or {}
        context = dict(self.context)
        # Keep chosen payloads as facts, without interpreting them as reward formulas.
        if valid and command != "init":
            context["selected_options:" + command] = sanitize(options)
        choice_fields = tuple("options." + str(key) for key in options)
        key = self.graph.observe(Observation(before, command, request, response, after,
            executed=True, outcome_observed=valid,
            validation="wire_state_observed; reward_not_validated" if valid else "failed_or_unresolved",
            context_before=self.context, context_after=context, prefix=tuple(self.prefix),
            choice_fields=choice_fields))
        if valid:
            self.previous = after
            self.context = context
            # A fresh spin begins a new replay prefix, retaining the bootstrap step.
            if len(self.prefix) < 128:
                self.prefix.append(key)
            else:
                self.graph.unknown("replay", key, "Prefix exceeds bounded trace; replay requires fresh bootstrap")
        else:
            self.graph.unknown("response", key, "No accepted flow.state; transition intentionally unresolved")
            # A failed operation may have reached the server; don't invent the next edge.
            self.previous = None
        if isinstance(response, dict):
            proof = self.graph.evidence("CLIENT_PROVEN", "tester_spin/providers/bgaming/runtime.py")
            for path, semantic in (("flow.state", "server_state"), ("flow.available_actions", "legal_actions"),
                                   ("balance.wallet", "wallet_balance_raw")):
                parent, name = path.split(".")
                if isinstance(response.get(parent), dict) and name in response[parent]:
                    sid = self.graph.key("semantic", path)
                    self.graph.data["field_semantics"][sid] = {"id": sid, "path": path,
                        "semantic": semantic, "confidence": "provider_parser", "evidence": [proof]}

    def discover(self, data: dict, evidence: dict | None = None) -> None:
        state = _state(data)
        if state is None:
            return
        from .flow_choices import _candidate_prompts
        for prompt in _candidate_prompts(data):
            choices = [Choice(label, prompt.option_payloads.get(label, {prompt.option_field: label}))
                       for label in prompt.available]
            self.graph.discover(state, prompt.command, choices, self.prefix)
        if not isinstance(evidence, dict):
            return
        fingerprint = evidence.get("client", {}).get("bundle_sha256")
        if fingerprint:
            self.fingerprints.add(fingerprint)
        proof = self.graph.evidence("CLIENT_PROVEN", "server-guided-discovery.json")
        for action in evidence.get("actions", []):
            command = str(action.get("action") or "")
            if not command:
                continue
            decision = self.graph.decision(state, command, proof, self.prefix)
            if not action.get("replay_eligible") or action.get("unresolved_option_variants"):
                self.graph.unknown("decision", decision, "Client request or option domain remains unresolved")
            for variant in action.get("option_variants", []):
                if not isinstance(variant.get("options"), dict):
                    continue
                source = "CLIENT_PROVEN" if action.get("replay_eligible") else "HEURISTIC"
                self.graph.discover(state, command, [Choice(str(variant.get("label") or command), variant["options"], source)], self.prefix)
            if action.get("parameterless") and action.get("replay_eligible"):
                self.graph.discover(state, command, [Choice(command, {})], self.prefix)
        # Retain machine-readable index-domain relations emitted by the UI bridge.
        for action in evidence.get("actions", []):
            domain = action.get("ui_index_domain")
            if isinstance(domain, dict):
                key = self.graph.key("relation", action.get("action"), domain)
                self.graph.data["relations"][key] = {"id": key, "command": action.get("action"),
                    "target": "request.options.index", "domain": sanitize(domain), "evidence": [proof]}
                self.graph.data["field_semantics"][key] = {"id": key, "path": domain.get("count_path"),
                    "semantic": "selectable_collection_size", "confidence": "client+runtime-proven", "evidence": [proof]}

    def finish(self, result, game_dir: Path) -> None:
        root = Path(result.run_dir) if result.run_dir else None
        if root is None or not root.is_dir():
            return
        if not self.graph.data["response_contracts"]:
            self.graph.unknown("protocol", "bgaming", "No supported JSON flow observations; protocol adapter required")
        if len(self.fingerprints) > 1:
            raise ValueError("Multiple client fingerprints in one run; refusing incompatible merge")
        fingerprint = next(iter(self.fingerprints), "")
        # Re-key all references consistently once client discovery has finished.
        if fingerprint:
            tables = self.graph.data
            keys = {key: identity(key.split(":", 1)[0], fingerprint, key)
                    for name, rows in tables.items() if isinstance(rows, dict) and name != "metadata"
                    for key in rows if ":" in key}
            def rekey(value):
                if isinstance(value, dict):
                    return {keys.get(k, k): rekey(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [rekey(v) for v in value]
                return keys.get(value, value) if isinstance(value, str) else value
            self.graph.data = rekey(tables)
            self.graph.scope = (*self.graph.scope[:3], fingerprint)
            self.graph.data["metadata"]["bundle_fingerprint"] = fingerprint
            self.graph.data["metadata"]["compatibility"] = "fingerprint_scoped"
        else:
            self.graph.unknown("client", "fingerprint", "Client compatibility unverified; only observed protocol evidence")
        self.graph.data["metadata"]["source_evidence_summary"] = {
            "capture": "BGaming JSON requests/responses at transport boundary",
            "diagnostics": self.diagnostics,
        }
        self.graph.data["artifact_references"] = [p.name for p in root.iterdir()
            if p.name in {"server-guided-discovery.json", "path-coverage.json", "sample-catalog.json", "outcome-catalog.json"}]
        payload = persist_map(game_dir / "analysis" / "game-structure.json", self.graph,
                              identity("run", root.name))
        for revision in payload["metadata"].get("previous_revisions", []):
            import json
            source = game_dir / "analysis" / revision["path"]
            atomic_write(root / "analysis" / revision["path"], json.loads(source.read_text(encoding="utf-8")))
        atomic_write(root / "analysis" / "game-structure.json", payload)
        result.structural_map = {"path": "analysis/game-structure.json", "schema": payload["schema"],
                                 "coverage": payload["coverage"], "diagnostics": list(self.diagnostics)}
        if self.diagnostics and result.status == "OK":
            result.status = "PARCIAL"


def begin_capture(slug: str):
    capture = Capture(StructuralMap("bgaming", "bgaming-json-flow", slug))
    return capture, _CURRENT.set(capture)


def end_capture(token) -> None:
    _CURRENT.reset(token)


def reset_capture() -> None:
    capture = _CURRENT.get()
    if capture:
        capture.reset()


def record_wire(request: dict, response: dict | None = None, *, failed: bool = False) -> None:
    capture = _CURRENT.get()
    if capture is not None:
        try:
            capture.wire(request, response, failed)
        except Exception as exc:
            # Exception values can contain payloads/credentials; persist only type.
            capture.diagnostics.append("wire capture: " + type(exc).__name__)


def record_discovery(data: dict, evidence: dict | None = None) -> None:
    capture = _CURRENT.get()
    if capture is not None:
        try:
            capture.discover(data, evidence)
        except Exception as exc:
            capture.diagnostics.append("choice capture: " + type(exc).__name__)
