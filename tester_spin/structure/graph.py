"""A cumulative control graph built only from explicit provider observations."""
from __future__ import annotations

from copy import deepcopy

from tester_spin.models import utc_now_iso
from .contracts import merge_schema, observe_schema
from .models import Choice, EVIDENCE_KINDS, Observation, SCHEMA, State
from .normalization import bounded_add, identity, sanitize

TABLES = ("states", "decision_points", "choices", "transitions", "request_contracts",
          "response_contracts", "field_semantics", "path_context", "unknowns", "evidence", "relations")


class StructuralMap:
    def __init__(self, provider: str, protocol_family: str, game: str, fingerprint: str = ""):
        self.data = {"schema": SCHEMA, "metadata": {
            "provider": provider, "protocol_family": protocol_family, "game_identifier": game,
            "slug": game, "bundle_fingerprint": fingerprint, "generated_at": utc_now_iso(),
            "compatibility": "fingerprint_scoped" if fingerprint else "unverified",
        }, **{name: {} for name in TABLES}}
        self.scope = (provider, protocol_family, game, fingerprint)

    @classmethod
    def load(cls, data: dict) -> StructuralMap:
        if data.get("schema") != SCHEMA or not all(isinstance(data.get(k), dict) for k in TABLES):
            raise ValueError("Unsupported or malformed structural map")
        meta = data["metadata"]
        result = cls(meta["provider"], meta["protocol_family"], meta["game_identifier"], meta["bundle_fingerprint"])
        result.data = deepcopy(data)
        return result

    def key(self, kind: str, *parts) -> str:
        return identity(kind, self.scope, *parts)

    def evidence(self, kind: str, detail: str = "structured observation") -> str:
        if kind not in EVIDENCE_KINDS:
            raise ValueError("Unknown evidence classification")
        key = self.key("evidence", kind, detail)
        self.data["evidence"].setdefault(key, {"id": key, "kind": kind, "source": sanitize(detail)})
        return key

    def unknown(self, kind: str, entity: str, reason: str) -> None:
        key = self.key("unknown", kind, entity, reason)
        self.data["unknowns"][key] = {"id": key, "kind": kind, "entity": entity,
                                      "status": "CONTRACT_UNRESOLVED", "reason": reason}

    def state(self, state: State, evidence: str, observed: bool = False) -> str:
        key = self.key("state", state.server_state, state.signature)
        row = self.data["states"].setdefault(key, {"id": key, "server_state": sanitize(state.server_state),
            "structural_signature": sanitize(state.signature), "terminal": state.terminal,
            "available_actions": [], "first_seen": utc_now_iso(), "observations": 0,
            "incoming_transitions": [], "outgoing_transitions": [], "response_contracts": [], "evidence": [],
            "unresolved": []})
        row["observations"] += int(observed)
        for action in state.available_actions:
            bounded_add(row["available_actions"], sanitize(action), 1000)
        bounded_add(row["evidence"], evidence, 32)
        if state.terminal is not None and row["terminal"] is not None and row["terminal"] != state.terminal:
            row["terminal"] = None
            self.unknown("state", key, "Conflicting terminal observations")
        return key

    def decision(self, state: State, command: str, evidence: str, prefix=()) -> str:
        sid = self.state(state, evidence)
        key = self.key("decision", sid, command)
        row = self.data["decision_points"].setdefault(key, {"id": key, "state_id": sid,
            "command": command, "type": "observed_action", "choices": [], "cardinality": {"known": 0, "exhaustive": False},
            "mutually_exclusive": None, "repeatable": None, "composite": None, "confirmation_required": None,
            "evidence": [], "replay_prefixes": [], "coverage": {}})
        bounded_add(row["evidence"], evidence, 32)
        bounded_add(row["replay_prefixes"], list(prefix), 32)
        return key

    def discover(self, state: State, command: str, choices: list[Choice], prefix=()) -> list[str]:
        keys = []
        for choice in choices:
            evidence = self.evidence(choice.source)
            decision = self.decision(state, command, evidence, prefix)
            key = self.key("choice", decision, choice.options)
            row = self.data["choices"].setdefault(key, {"id": key, "decision_point_id": decision,
                "label": sanitize(choice.label), "semantic_value": None,
                "request": sanitize({"command": command, "options": choice.options}),
                "request_contracts": [], "evidence": [], "preconditions": [], "context_mutations": [],
                "coverage": {"discovered": True, "executed": False, "outcome_observed": False,
                             "outcome_derived": False, "attempts": 0, "successful_samples": 0},
                "unresolved_reason": "No wire outcome observed"})
            bounded_add(row["evidence"], evidence, 32)
            bounded_add(self.data["decision_points"][decision]["choices"], key, 10000)
            if choice.source in {"HEURISTIC", "SERVER_ADVERTISED"}:
                self.unknown("choice", key, "Discovery does not authorize replay")
            keys.append(key)
        return keys

    def contract(self, table: str, scope: tuple, sample, evidence: str, roles=None) -> str:
        key = self.key(table, *scope)
        row = self.data[table].setdefault(key, {"id": key, "schema": {}, "examples": [],
            "observations": 0, "evidence": [], "status": "observed_partial", "conditional_fields": []})
        observe_schema(row["schema"], sample, roles=roles)
        row["observations"] += 1
        # Bound individual examples too; schemas preserve complete nesting.
        clean = sanitize(sample)
        from .normalization import canonical
        if len(canonical(clean)) <= 8192:
            bounded_add(row["examples"], clean)
        bounded_add(row["evidence"], evidence, 32)
        return key

    def observe(self, event: Observation) -> str:
        evidence = self.evidence(event.evidence_kind)
        sid = self.state(event.before, evidence)
        options = event.request.get("options", {})
        choice = self.discover(event.before, event.command,
            [Choice(event.command, options, event.evidence_kind)], event.prefix)[0]
        row = self.data["choices"][choice]
        decision = row["decision_point_id"]
        roles = {"command": "static", **{path: "choice" for path in event.choice_fields}}
        request = self.contract("request_contracts", (sid, event.command), event.request, evidence, roles)
        bounded_add(row["request_contracts"], request, 100)
        coverage = row["coverage"]
        coverage["executed"] |= event.executed
        coverage["attempts"] += int(event.executed)
        coverage["outcome_observed"] |= event.outcome_observed
        coverage["successful_samples"] += int(event.outcome_observed)
        context_before, context_after = sanitize(event.context_before), sanitize(event.context_after)
        mutation = {k: v for k, v in context_after.items() if context_before.get(k) != v}
        context_key = self.key("context", context_before, context_after)
        self.data["path_context"][context_key] = {"id": context_key, "before": context_before,
            "mutation": mutation, "after": context_after}
        bounded_add(row["context_mutations"], mutation, 32)
        response = None
        to_state = None
        if event.response is not None:
            response = self.contract("response_contracts", (sid, event.command, event.after.server_state if event.after else "unresolved"), event.response, evidence)
        if not event.outcome_observed or event.after is None:
            self.unknown("choice", choice, "No validated next-state outcome for one or more attempts")
            return choice
        row["unresolved_reason"] = ""
        to_state = self.state(event.after, evidence, observed=True)
        bounded_add(self.data["states"][to_state]["response_contracts"], response, 100)
        transition = self.key("transition", sid, decision, choice, request, response, to_state, context_key)
        edge = self.data["transitions"].setdefault(transition, {"id": transition, "from_state": sid,
            "decision_point": decision, "choice": choice, "request_contract": request,
            "response_contract": response, "to_state": to_state, "observations": 0, "evidence": [],
            "terminal": event.after.terminal, "path_context": context_key, "validation_results": {}})
        edge["observations"] += 1
        edge["validation_results"][event.validation] = edge["validation_results"].get(event.validation, 0) + 1
        bounded_add(edge["evidence"], evidence, 32)
        bounded_add(self.data["states"][sid]["outgoing_transitions"], transition, 10000)
        bounded_add(self.data["states"][to_state]["incoming_transitions"], transition, 10000)
        return choice

    def merge(self, other: StructuralMap) -> None:
        if self.scope != other.scope:
            raise ValueError("Incompatible provider/game/protocol/fingerprint")
        for name in TABLES:
            target = self.data[name]
            for key, incoming in other.data[name].items():
                if key not in target:
                    target[key] = deepcopy(incoming)
                    continue
                row = target[key]
                if "observations" in row:
                    row["observations"] += incoming["observations"]
                if name.endswith("contracts"):
                    merge_schema(row["schema"], incoming["schema"])
                if name == "choices":
                    for field, value in incoming["coverage"].items():
                        if isinstance(value, bool):
                            row["coverage"][field] |= value
                        else:
                            row["coverage"][field] += value
                    if incoming["coverage"]["outcome_observed"]:
                        row["unresolved_reason"] = ""
                for field, value in incoming.items():
                    if isinstance(value, list):
                        for item in value:
                            bounded_add(row.setdefault(field, []), deepcopy(item), 3 if field == "examples" else 10000)
                if name == "transitions":
                    for value, count in incoming["validation_results"].items():
                        row["validation_results"][value] = row["validation_results"].get(value, 0) + count
                if name == "states" and row["terminal"] != incoming["terminal"]:
                    row["terminal"] = None
        self.data["metadata"]["generated_at"] = utc_now_iso()

    def to_dict(self) -> dict:
        semantic_paths = {item["path"] for item in self.data["field_semantics"].values()}
        def unresolved_fields(contract, node, path=""):
            if not node.get("properties") and "items" not in node and node.get("role") == "unknown" and path not in semantic_paths:
                self.unknown("field", contract + "/" + path, "Semantic meaning unknown")
            for name, child in node.get("properties", {}).items():
                unresolved_fields(contract, child, f"{path}.{name}".strip("."))
            if "items" in node:
                unresolved_fields(contract, node["items"], path + "[]")
        for contract in self.data["response_contracts"].values():
            unresolved_fields(contract["id"], contract["schema"])
        for decision in self.data["decision_points"].values():
            choices = [self.data["choices"][key] for key in decision["choices"]]
            decision["cardinality"]["known"] = len(choices)
            decision["coverage"] = {"discovered": len(choices),
                "executed": sum(c["coverage"]["executed"] for c in choices),
                "outcome_observed": sum(c["coverage"]["outcome_observed"] for c in choices)}
        choices = list(self.data["choices"].values())
        self.data["coverage"] = {"states": len(self.data["states"]), "decision_points": len(self.data["decision_points"]),
            "choices": len(choices), "executed": sum(c["coverage"]["executed"] for c in choices),
            "outcome_observed": sum(c["coverage"]["outcome_observed"] for c in choices),
            "pending": sum(not c["coverage"]["outcome_observed"] for c in choices),
            "unknowns": len(self.data["unknowns"]), "complete": False,
            "completeness_basis": "Observed evidence only; domains and conditional fields may be incomplete"}
        return deepcopy(self.data)
