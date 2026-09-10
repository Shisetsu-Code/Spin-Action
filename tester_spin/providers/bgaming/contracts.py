from __future__ import annotations

# BGaming continuation commands whose wire payload has been observed to require
# no game-specific parameters beyond the session state. This is provider-level
# protocol vocabulary, not a per-title allowlist.
SAFE_CONTINUATION_COMMANDS = frozenset(
    {
        "freespin",
        "respin",
        "play_bonus",
        "play_preselection_game",
        "close",
    }
)

# State -> command aliases confirmed by provider protocol captures.
CONTINUATION_BY_STATE = {
    "freespins": "freespin",
    "respin": "respin",
    "play_bonus": "play_bonus",
    "preselection_game": "play_preselection_game",
    "gamble": "close",
}
