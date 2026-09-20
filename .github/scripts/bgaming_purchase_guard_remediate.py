from __future__ import annotations

import json
from pathlib import Path


TARGET = Path("tester_spin/providers/bgaming_paths_v2.py")
TEST = Path("tests/test_bgaming_path_policy.py")
CONFIG = Path(".github/provider-validation.json")


OLD_GUARD = '''def _purchase_modes_guard(data: dict[str, Any]) -> list[dict[str, Any]]:
    advertised = _policy._ORIGINAL_DISCOVER_PURCHASE_MODES(data)
    if not _coverage_active():
        return advertised

    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    dynamic_seen = hasattr(_policy._LOCAL, "dynamic_purchased_feature")
    if not isinstance(explicit, set) and not dynamic_seen:
        return advertised

    return [mode for mode in advertised if _purchase_mode_authorized(mode)]
'''

NEW_GUARD = '''def _purchase_modes_guard(
    data: dict[str, Any],
    *,
    selector_domains: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    # Keep the policy guard signature compatible with runtime discovery and
    # forward the exact client-proven selector domain unchanged. The policy may
    # filter advertised purchases, but it must never erase evidence required to
    # distinguish a selector-scoped price table from a feature-level domain.
    advertised = _policy._ORIGINAL_DISCOVER_PURCHASE_MODES(
        data,
        selector_domains=selector_domains,
    )
    if not _coverage_active():
        return advertised

    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    dynamic_seen = hasattr(_policy._LOCAL, "dynamic_purchased_feature")
    if not isinstance(explicit, set) and not dynamic_seen:
        return advertised

    return [mode for mode in advertised if _purchase_mode_authorized(mode)]
'''

TEST_IMPORT_OLD = "import unittest\nfrom pathlib import Path\n"
TEST_IMPORT_NEW = (
    "import unittest\n"
    "from pathlib import Path\n"
    "from unittest.mock import patch\n\n"
    "import tester_spin.providers.bgaming_paths_v2 as bgaming_paths_v2\n"
)

TEST_METHOD = '''
    def test_purchase_modes_guard_forwards_selector_domains(self) -> None:
        observed: dict[str, object] = {}

        def fake_discover(data, *, selector_domains=None):
            observed["data"] = data
            observed["selector_domains"] = selector_domains
            return [{"name": "freespin_buy", "level": None}]

        init_data = {"options": {}}
        domains = {"rows": [3, 4, 5]}
        with (
            patch.object(
                bgaming_paths_v2._policy,
                "_ORIGINAL_DISCOVER_PURCHASE_MODES",
                side_effect=fake_discover,
            ),
            patch.object(bgaming_paths_v2, "_coverage_active", return_value=False),
        ):
            modes = bgaming_paths_v2._purchase_modes_guard(
                init_data,
                selector_domains=domains,
            )

        self.assertEqual(observed["data"], init_data)
        self.assertEqual(observed["selector_domains"], domains)
        self.assertEqual(modes, [{"name": "freespin_buy", "level": None}])
'''


def main() -> None:
    changed = False

    target_text = TARGET.read_text(encoding="utf-8")
    if OLD_GUARD in target_text:
        TARGET.write_text(
            target_text.replace(OLD_GUARD, NEW_GUARD, 1),
            encoding="utf-8",
        )
        changed = True
    elif "selector_domains: dict[str, list[Any]] | None = None" not in target_text:
        raise SystemExit("BGaming purchase guard context changed; refusing blind patch")

    test_text = TEST.read_text(encoding="utf-8")
    marker = "def test_purchase_modes_guard_forwards_selector_domains"
    if marker not in test_text:
        if TEST_IMPORT_OLD not in test_text:
            raise SystemExit("BGaming path-policy test import context changed")
        test_text = test_text.replace(TEST_IMPORT_OLD, TEST_IMPORT_NEW, 1)
        anchor = '\n\nif __name__ == "__main__":\n'
        if anchor not in test_text:
            raise SystemExit("BGaming path-policy test footer context changed")
        test_text = test_text.replace(anchor, TEST_METHOD + anchor, 1)
        TEST.write_text(test_text, encoding="utf-8")
        changed = True

    if changed:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        data["run_sequence"] = int(data.get("run_sequence") or 0) + 1
        CONFIG.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
