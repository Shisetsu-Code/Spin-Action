from __future__ import annotations

from tester_spin.providers.bgaming.ui_index_domain import (
    augment_evidence_with_ui_index_domain,
)


def _data() -> dict:
    return {
        "flow": {
            "state": "pick_cards",
            "available_actions": ["init", "pick_cards"],
        },
        "features": {
            "freespins_issued": 8,
            "cards_data": {
                "issued": 3,
                "list": [],
                "new": [],
            },
        },
    }


def _evidence() -> dict:
    return {
        "flow": {
            "state": "pick_cards",
            "available_actions": ["init", "pick_cards"],
        },
        "actions": [
            {
                "action": "pick_cards",
                "serializer_shape_proven": True,
                "replay_eligible": True,
                "execution_authority": "evidence-only",
                "option_fields": ["mode", "index"],
                "option_variants": [
                    {
                        "label": 'mode="select_pick_cards"',
                        "options": {"mode": "select_pick_cards"},
                        "source": "client-callsite:requestCardsPick",
                    },
                    {
                        "label": 'mode="auto"',
                        "options": {"mode": "auto"},
                        "source": "client-callsite:requestCardsPick",
                    },
                ],
                "unresolved_option_variants": [
                    {
                        "literal_options": {"mode": "any"},
                        "unresolved_fields": ["index"],
                        "source": "client-callsite:requestCardsPick",
                    }
                ],
            }
        ],
    }


def _bundle() -> str:
    return """
    requestCardsPick=async e=>q.request({command:"pick_cards",options:e});
    onPlayer=async e=>this.requestCardsPick({mode:"any",index:e});
    get index(){return this.parent.parent.children.indexOf(this.parent)}
    enableAllCardsInteractive=e=>{
      this.second_clip.getChildByName("cards-list").children.forEach(t=>t.children[0].enable(e))
    };
    disableAllCardsInteractive=e=>{
      this.second_clip.getChildByName("cards-list").children.forEach(t=>t.children[0].disable(e))
    };
    renderCards(){const count=this.features.cards_data.issued;return count}
    """


def test_scene_graph_position_and_correlated_runtime_count_resolve_indices() -> None:
    enriched = augment_evidence_with_ui_index_domain(
        _data(),
        _evidence(),
        _bundle(),
    )
    action = enriched["actions"][0]

    assert action["ui_index_domain"] == {
        "collection": "cards-list",
        "count_path": "$.features.cards_data.issued",
        "count": 3,
        "indices": [0, 1, 2],
        "source": "client-scene-graph+server-runtime",
    }
    assert action["unresolved_option_variants"] == []
    assert action["execution_authority"] == "client+runtime-proven"

    options = [item["options"] for item in action["option_variants"]]
    assert {"mode": "any", "index": 0} in options
    assert {"mode": "any", "index": 1} in options
    assert {"mode": "any", "index": 2} in options
    assert {"mode": "any", "index": 3} not in options


def test_server_count_without_client_index_semantics_does_not_authorize_replay() -> None:
    bundle = (
        'requestCardsPick=async e=>q.request({command:"pick_cards",options:e});'
        'onPlayer=async e=>this.requestCardsPick({mode:"any",index:e});'
        'renderCards(){return this.features.cards_data.issued}'
    )
    original = _evidence()
    enriched = augment_evidence_with_ui_index_domain(_data(), original, bundle)

    assert enriched == original


def test_client_index_semantics_without_correlated_runtime_count_stays_unresolved() -> None:
    data = {
        "flow": {"state": "pick_cards", "available_actions": ["pick_cards"]},
        "features": {"other_count": 3},
    }
    original = _evidence()
    enriched = augment_evidence_with_ui_index_domain(data, original, _bundle())

    assert enriched == original
