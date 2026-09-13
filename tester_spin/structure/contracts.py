"""Incremental evidence schemas. Required means observed presence, never proof."""
from __future__ import annotations

from typing import Any

from .normalization import bounded_add, field_role, safe_key, sanitize


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object" if isinstance(value, dict) else "array"


def observe_schema(node: dict, value: Any, path: str = "", roles: dict | None = None) -> None:
    roles = roles or {}
    node["samples"] = node.get("samples", 0) + 1
    types = node.setdefault("observed_types", {})
    kind = type_name(value)
    types[kind] = types.get(kind, 0) + 1
    node["nullable"] = "null" in types
    node["confidence"] = "observed_only"
    role = field_role(path)
    node["role"] = role if role != "unknown" else roles.get(path, "unknown")
    if isinstance(value, dict):
        properties = node.setdefault("properties", {})
        for key, child in value.items():
            if safe_key(key) != str(key):
                node["mapping_keys"] = "runtime identifiers"
                observe_schema(node.setdefault("mapping_values", {}), child, path + ".*", roles)
                continue
            key = safe_key(key)
            observe_schema(properties.setdefault(key, {}), child, f"{path}.{key}".strip("."), roles)
        for child in properties.values():
            child["parent_samples"] = types["object"]
            child["optional_observed"] = child["samples"] < types["object"]
            child["present_in_all_observed"] = not child["optional_observed"]
            child["required"] = None
        node.setdefault("mapping_keys", "observed_properties; dynamic-key semantics unresolved")
    elif isinstance(value, (list, tuple)):
        lengths = node.setdefault("length", {"min": len(value), "max": len(value)})
        lengths["min"] = min(lengths["min"], len(value))
        lengths["max"] = max(lengths["max"], len(value))
        node["position_semantics"] = "unknown"
        node["collection_role"] = "unknown; does not establish choices"
        for child in value:
            observe_schema(node.setdefault("items", {}), child, path + "[]", roles)
    else:
        examples = node.setdefault("observed_values", [])
        clean = sanitize(value, path)
        bounded_add(examples, clean, 8)
        node["values_bounded"] = True
        node["variability"] = "variable" if len(examples) > 1 else "constant_in_observations"
        node["enum_exhaustive"] = False


def merge_schema(target: dict, source: dict) -> None:
    if not target:
        from copy import deepcopy
        target.update(deepcopy(source))
        return
    target["samples"] += source["samples"]
    for kind, count in source["observed_types"].items():
        target["observed_types"][kind] = target["observed_types"].get(kind, 0) + count
    target["nullable"] = "null" in target["observed_types"]
    if target["role"] == "unknown":
        target["role"] = source["role"]
    for value in source.get("observed_values", []):
        bounded_add(target.setdefault("observed_values", []), value, 8)
    if "observed_values" in target:
        target["variability"] = "variable" if len(target["observed_values"]) > 1 else "constant_in_observations"
    if "properties" in source or "properties" in target:
        properties = target.setdefault("properties", {})
        for key, child in source.get("properties", {}).items():
            merge_schema(properties.setdefault(key, {}), child)
        for child in properties.values():
            child["parent_samples"] = target["observed_types"]["object"]
            child["optional_observed"] = child["samples"] < child["parent_samples"]
            child["present_in_all_observed"] = not child["optional_observed"]
            child["required"] = None
    if "length" in source:
        length = target.setdefault("length", dict(source["length"]))
        length["min"] = min(length["min"], source["length"]["min"])
        length["max"] = max(length["max"], source["length"]["max"])
        target.setdefault("position_semantics", "unknown")
        target.setdefault("collection_role", "unknown; does not establish choices")
    if "items" in source:
        merge_schema(target.setdefault("items", {}), source["items"])
    if "mapping_values" in source:
        target["mapping_keys"] = "runtime identifiers"
        merge_schema(target.setdefault("mapping_values", {}), source["mapping_values"])
