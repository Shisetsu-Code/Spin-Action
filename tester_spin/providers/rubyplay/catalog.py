from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from tester_spin.models import Game


@dataclass(slots=True)
class BricksCatalogState:
    rest_api_url: str
    nonce: str
    wp_rest_nonce: str
    post_id: str
    language: str
    query_element_id: str
    query_vars: dict[str, Any]
    page: int
    max_pages: int
    start: int
    end: int
    candidate_count: int = 1

    @property
    def load_query_url(self) -> str:
        return urljoin(self.rest_api_url.rstrip("/") + "/", "load_query_page")


@dataclass(slots=True)
class RubyPlayCatalogRecord:
    game: Game
    theme: str = ""


def _js_object_block(html: str, name: str) -> str:
    match = re.search(
        rf"(?:window\.)?{re.escape(name)}\s*=\s*\{{(?P<body>.*?)\}}\s*;",
        html or "",
        re.S,
    )
    return match.group("body") if match else ""


def _js_string(block: str, key: str) -> str:
    match = re.search(
        rf"\b{re.escape(key)}\s*:\s*(['\"])(.*?)\1",
        block or "",
        re.S,
    )
    return str(match.group(2)).strip() if match else ""


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def query_loop_html(html: str, query_element_id: str) -> str:
    """Return only the rendered Bricks loop for one dynamic element ID.

    Bricks emits brx-loop-start/end comments for AJAX/query features. When an
    optimiser strips comments, repeated loop roots still normally carry the
    Bricks element ID as a ``brxe-<id>`` class, so that is used as a fallback.
    """
    query_id = str(query_element_id or "").strip()
    if not query_id:
        return ""

    marker = re.search(
        rf"<!--\s*brx-loop-start-{re.escape(query_id)}\s*-->(.*?)"
        rf"<!--\s*brx-loop-end-{re.escape(query_id)}\s*-->",
        html or "",
        re.S | re.I,
    )
    if marker:
        return marker.group(1)

    soup = BeautifulSoup(html or "", "html.parser")
    wanted_class = f"brxe-{query_id}"
    pieces: list[str] = []
    for node in soup.find_all(True):
        classes = node.get("class") or []
        if wanted_class in {str(value) for value in classes}:
            pieces.append(str(node))
    return "\n".join(pieces)


def _unique_game_link_count(fragment: str, page_url: str) -> int:
    if not fragment:
        return 0
    soup = BeautifulSoup(fragment, "html.parser")
    slugs: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(page_url, str(anchor.get("href") or ""))
        path = urlparse(absolute).path or ""
        match = re.fullmatch(r"/games/([^/]+)/?", path)
        if match and match.group(1).strip():
            slugs.add(match.group(1).strip().lower())
    return len(slugs)


def _query_candidate_score(
    html: str,
    page_url: str,
    node,
    query_vars: dict[str, Any],
) -> tuple[int, int, int, int, int]:
    query_id = str(
        node.get("data-query-element-id")
        or node.get("data-element-id")
        or ""
    ).strip()
    page = max(1, _int_value(node.get("data-page"), 1))
    max_pages = max(1, _int_value(node.get("data-max-pages"), 1))
    start = max(1, _int_value(node.get("data-start"), 1))
    end = max(0, _int_value(node.get("data-end"), 0))
    span = max(0, end - start + 1) if end >= start else 0
    per_page = max(0, _int_value(query_vars.get("posts_per_page"), 0))
    rendered_count = _unique_game_link_count(
        query_loop_html(html, query_id),
        page_url,
    )

    # The archive query is expected to describe the largest coherent game set,
    # not merely be the first post_type=games loop. No generated Bricks ID or
    # RubyPlay title is involved in this ranking.
    page_width = max(rendered_count, span, per_page, 1)
    estimated_capacity = max(end, page_width * max_pages)
    current_page_matches = int(
        _int_value(query_vars.get("paged"), page) in {0, page}
    )
    return (
        estimated_capacity,
        max_pages,
        rendered_count,
        page_width,
        current_page_matches,
    )


def parse_bricks_catalog_state(html: str, page_url: str) -> BricksCatalogState:
    soup = BeautifulSoup(html or "", "html.parser")
    trails: list[tuple[Any, dict[str, Any]]] = []
    for node in soup.find_all(attrs={"data-query-vars": True}):
        raw = str(node.get("data-query-vars") or "").strip()
        try:
            query_vars = json.loads(raw)
        except Exception:
            continue
        post_type = query_vars.get("post_type") if isinstance(query_vars, dict) else None
        if isinstance(post_type, str):
            post_types = {post_type}
        elif isinstance(post_type, list):
            post_types = {str(x) for x in post_type}
        else:
            post_types = set()
        if "games" not in post_types:
            continue
        trails.append((node, query_vars))

    if not trails:
        raise ValueError("RubyPlay catálogo: no se encontró query Bricks post_type=games.")

    ranked = [
        (
            _query_candidate_score(html, page_url, node, query_vars),
            node,
            query_vars,
        )
        for node, query_vars in trails
    ]
    best_score = max(score for score, _node, _vars in ranked)
    winners = [
        (node, query_vars)
        for score, node, query_vars in ranked
        if score == best_score
    ]
    if len(winners) != 1:
        summary = ", ".join(
            f"{str(node.get('data-query-element-id') or node.get('data-element-id') or '?')}:{score}"
            for score, node, _query_vars in sorted(ranked, key=lambda item: item[0], reverse=True)[:8]
        )
        raise ValueError(
            "RubyPlay catálogo: varios query Bricks post_type=games siguen "
            f"indistinguibles ({len(winners)}/{len(trails)}); candidatos={summary}."
        )

    node, query_vars = winners[0]
    query_element_id = str(
        node.get("data-query-element-id")
        or node.get("data-element-id")
        or ""
    ).strip()
    if not query_element_id:
        raise ValueError("RubyPlay catálogo: falta data-query-element-id.")

    bricks = _js_object_block(html, "bricksData")
    rest_api_url = _js_string(bricks, "restApiUrl")
    nonce = _js_string(bricks, "nonce")
    wp_rest_nonce = _js_string(bricks, "wpRestNonce")
    post_id = _js_string(bricks, "postId")
    language = _js_string(bricks, "language") or "en"
    if not rest_api_url:
        raise ValueError("RubyPlay catálogo: falta bricksData.restApiUrl.")
    if not urlparse(rest_api_url).scheme:
        rest_api_url = urljoin(page_url, rest_api_url)

    def int_attr(name: str, default: int) -> int:
        return _int_value(node.get(name), default)

    return BricksCatalogState(
        rest_api_url=rest_api_url,
        nonce=nonce,
        wp_rest_nonce=wp_rest_nonce,
        post_id=post_id,
        language=language,
        query_element_id=query_element_id,
        query_vars=dict(query_vars),
        page=max(1, int_attr("data-page", 1)),
        max_pages=max(1, int_attr("data-max-pages", 1)),
        start=max(1, int_attr("data-start", 1)),
        end=max(0, int_attr("data-end", 0)),
        candidate_count=len(trails),
    )


def _game_href(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    match = re.fullmatch(r"/games/([^/]+)/?", parsed.path or "")
    if not match:
        return None
    slug = match.group(1).strip().lower()
    return (slug, parsed.path) if slug else None


def _img_source(img) -> str:
    for key in ("data-src", "src", "data-lazy-src"):
        value = str(img.get(key) or "").strip()
        if value and not value.lower().startswith("data:"):
            return value
    return ""


def parse_catalog_html(html: str, base_url: str) -> list[RubyPlayCatalogRecord]:
    soup = BeautifulSoup(html or "", "html.parser")
    raw: dict[str, dict[str, Any]] = {}

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base_url, str(anchor.get("href") or ""))
        parsed = _game_href(absolute)
        if parsed is None:
            continue
        slug, _path = parsed
        record = raw.setdefault(
            slug,
            {
                "url": absolute,
                "name": "",
                "thumbnail_url": "",
                "theme": "",
            },
        )
        text = " ".join(anchor.stripped_strings).strip()
        if text and len(text) > len(record["name"]):
            record["name"] = text
        img = anchor.find("img")
        if img is not None:
            source = _img_source(img)
            if source:
                record["thumbnail_url"] = urljoin(base_url, source)

        container = anchor.find_parent(["article", "li", "div"])
        if container is not None:
            for related in container.find_all("a", href=True):
                related_url = urljoin(base_url, str(related.get("href") or ""))
                if re.fullmatch(r"/game_theme/[^/]+/?", urlparse(related_url).path or ""):
                    theme = " ".join(related.stripped_strings).strip()
                    if theme:
                        record["theme"] = theme
                        break

    out: list[RubyPlayCatalogRecord] = []
    for slug, record in raw.items():
        name = str(record["name"] or "").strip()
        if not name:
            name = slug.replace("-", " ").title()
        out.append(
            RubyPlayCatalogRecord(
                game=Game(
                    provider="rubyplay",
                    slug=slug,
                    name=name,
                    url=str(record["url"]),
                    thumbnail_url=str(record["thumbnail_url"] or ""),
                ),
                theme=str(record["theme"] or ""),
            )
        )
    return sorted(out, key=lambda item: item.game.name.casefold())


def load_query_payload(state: BricksCatalogState, page: int) -> dict[str, Any]:
    return {
        "postId": state.post_id,
        "queryElementId": state.query_element_id,
        "componentId": False,
        "queryVars": json.dumps(state.query_vars, ensure_ascii=False, separators=(",", ":")),
        "page": int(page),
        "nonce": state.nonce,
        "lang": state.language,
        "mainQueryId": False,
    }


def updated_query_meta(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("updated_query")
    if not isinstance(raw, dict):
        raise ValueError("RubyPlay catálogo: respuesta sin updated_query.")
    out: dict[str, int] = {}
    for source, target in (
        ("count", "count"),
        ("max_num_pages", "max_pages"),
        ("start", "start"),
        ("end", "end"),
    ):
        try:
            out[target] = int(raw.get(source))
        except (TypeError, ValueError):
            raise ValueError(f"RubyPlay catálogo: updated_query.{source} inválido.")
    return out
