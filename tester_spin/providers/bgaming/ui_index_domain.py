from __future__ import annotations

import copy
import re
from typing import Any


_COUNT_HINTS = {
    "amount",
    "count",
    "issued",
    "length",
    "number",
    "size",
    "total",
}
_MAX_UI_CHOICES = 64


def _word_stems(value: str) -> set[str]:
    stems: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9]+", str(value or "").casefold()):
        if len(raw) < 3:
            continue
        stems.add(raw)
        if raw.endswith("s") and len(raw) > 3:
            stems.add(raw[:-1])
    return stems


def _client_index_collections(bundle: str) -> list[str]:
    """Return scene-graph child collections whose members expose zero-based indices."""
    text = str(bundle or "")
    if not text:
        return []

    # Strong proof that the component index is literally its position in the
    # parent's children array. This is intentionally structural, not title based.
    index_semantics = bool(
        re.search(
            r"parent\.parent\.children\.indexOf\(\s*this\.parent\s*\)",
            text,
        )
    )
    if not index_semantics:
        return []

    collections: list[str] = []
    pattern = re.compile(
        r"getChildByName\(\s*[\"']([^\"']+)[\"']\s*\)\.children"
    )
    for match in pattern.finditer(text):
        name = str(match.group(1) or "").strip()
        if name and name not in collections:
            collections.append(name)
    return collections


def _positive_integer_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for raw_key, item in list(value.items())[:100]:
                key = str(raw_key)
                child = f"{path}.{key}"
                if (
                    isinstance(item, int)
                    and not isinstance(item, bool)
                    and 0 < item <= _MAX_UI_CHOICES
                    and key.casefold() in _COUNT_HINTS
                ):
                    candidates.append(
                        {
                            "path": child,
                            "key": key,
                            "value": int(item),
                            "stems": _word_stems(child),
                        }
                    )
                elif isinstance(item, (dict, list)):
                    walk(item, child, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:80]):
                if isinstance(item, (dict, list)):
                    walk(item, f"{path}[{index}]", depth + 1)

    for root in ("features", "game", "options"):
        value = data.get(root)
        if isinstance(value, (dict, list)):
            walk(value, f"$.{root}", 0)
    return candidates


def _best_count_for_collection(
    data: dict[str, Any],
    bundle: str,
    collection: str,
) -> dict[str, Any] | None:
    collection_stems = _word_stems(collection)
    if not collection_stems:
        return None

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for candidate in _positive_integer_candidates(data):
        shared = collection_stems.intersection(candidate["stems"])
        if not shared:
            continue
        score = 4 * len(shared)
        key = str(candidate["key"])
        if re.search(r"\b" + re.escape(key) + r"\b", bundle or ""):
            score += 2
        # Prefer response objects whose semantic path is also named in the
        # client bundle (e.g. cards_data + issued), without requiring a game title.
        path_tokens = [
            token
            for token in re.split(r"[^A-Za-z0-9_]+", str(candidate["path"]))
            if len(token) >= 3 and token not in {"features", "game", "options"}
        ]
        score += sum(1 for token in path_tokens if token in (bundle or ""))
        ranked.append((score, str(candidate["path"]), candidate))

    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score = ranked[0][0]
    best = [item for item in ranked if item[0] == best_score]
    if len(best) != 1:
        return None
    return dict(best[0][2])


def _variant_label(options: dict[str, Any]) -> str:
    def render(value: Any) -> str:
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if value is True:
            return "true"
        if value is False:
            return "false"
        if value is None:
            return "null"
        return str(value)

    return "|".join(
        f"{key}={render(options[key])}"
        for key in sorted(options)
    )


def _merge_variant(target: list[dict[str, Any]], item: dict[str, Any]) -> None:
    options = item.get("options")
    if not isinstance(options, dict):
        return
    for existing in target:
        if isinstance(existing, dict) and existing.get("options") == options:
            return
    target.append(dict(item))


def augment_evidence_with_ui_index_domain(
    data: dict[str, Any],
    evidence: dict[str, Any],
    bundle: str,
) -> dict[str, Any]:
    """Resolve forwarded ``index`` options from client scene-graph evidence.

    Promotion is deliberately narrow. A server count by itself is insufficient,
    and a client ``index`` parameter by itself is insufficient. We require:

    * an unresolved client-proven variant whose only unknown field is ``index``;
    * client code proving component.index == parent.children.indexOf(component);
    * a named child collection in the client scene graph; and
    * exactly one positive runtime count correlated with that collection.

    The returned evidence remains provider-generic and contains no game-title rule.
    """
    if not isinstance(data, dict) or not isinstance(evidence, dict) or not bundle:
        return evidence

    collections = _client_index_collections(bundle)
    if not collections:
        return evidence

    resolved_domains: list[dict[str, Any]] = []
    for collection in collections:
        count = _best_count_for_collection(data, bundle, collection)
        if count is not None:
            resolved_domains.append(
                {
                    "collection": collection,
                    "count_path": str(count["path"]),
                    "count": int(count["value"]),
                    "indices": list(range(int(count["value"]))),
                    "source": "client-scene-graph+server-runtime",
                }
            )

    if len(resolved_domains) != 1:
        return evidence
    domain = resolved_domains[0]

    enriched = copy.deepcopy(evidence)
    actions = enriched.get("actions")
    if not isinstance(actions, list):
        return evidence

    for action in actions:
        if not isinstance(action, dict):
            continue
        unresolved = action.get("unresolved_option_variants")
        if not isinstance(unresolved, list) or not unresolved:
            continue

        retained: list[dict[str, Any]] = []
        promoted = False
        variants = action.get("option_variants")
        if not isinstance(variants, list):
            variants = []
            action["option_variants"] = variants

        for raw in unresolved:
            if not isinstance(raw, dict):
                continue
            unresolved_fields = [str(item) for item in raw.get("unresolved_fields") or []]
            literals = raw.get("literal_options")
            source = str(raw.get("source") or "")
            if unresolved_fields != ["index"] or not isinstance(literals, dict):
                retained.append(raw)
                continue
            if not source.startswith("client-callsite:"):
                retained.append(raw)
                continue

            for index in domain["indices"]:
                options = dict(literals)
                options["index"] = int(index)
                _merge_variant(
                    variants,
                    {
                        "label": _variant_label(options),
                        "options": options,
                        "source": (
                            source
                            + "+client-scene-graph+server-runtime:"
                            + str(domain["count_path"])
                        ),
                    },
                )
            promoted = True

        action["unresolved_option_variants"] = retained
        if promoted:
            action["ui_index_domain"] = dict(domain)
            action["replay_eligible"] = bool(
                action.get("serializer_shape_proven") and variants
            )
            action["execution_authority"] = "client+runtime-proven"

    return enriched


__all__ = ["augment_evidence_with_ui_index_domain"]
