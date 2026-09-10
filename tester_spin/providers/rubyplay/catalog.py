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
    """Extract a JS object assignment without assuming var/window formatting.

    Bricks has emitted ``bricksData = {...}``, ``var bricksData = {...}`` and
    ``window.bricksData = {...}`` across builds. A balanced scanner is safer
    than stopping at the first ``};`` because the object can contain nested
    values.
    """
    match = re.search(
        rf"(?:(?:var|let|const)\s+)?(?:window\.)?{re.escape(name)}\s*=\s*\{{",
        html or "",
        re.I,
    )
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1 : index]
    return ""


def _js_string(block: str, key: str) -> str:
    match = re.search(
        rf"(?:['\"])?{re.escape(key)}(?:['\"])?\s*:\s*(['\"])(.*?)\1",
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
    """Return only the rendered Bricks loop for one dynamic element ID."""
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


def _rest_api_url_from_page(html: str, page_url: str) -> str:
    bricks = _js_object_block(html, "bricksData")
    rest_api_url = _js_string(bricks, "restApiUrl")
    if rest_api_url:
        return (
            rest_api_url
            if urlparse(rest_api_url).scheme
            else urljoin(page_url, rest_api_url)
        )

    # WordPress publishes its REST root independently of Bricks. Prefer that
    # structural signal when a cache/minifier omits the bricksData assignment.
    soup = BeautifulSoup(html or "", "html.parser")
    for link in soup.find_all("link", href=True):
        rel = {str(value).lower() for value in (link.get("rel") or [])}
        if "https://api.w.org/" not in rel:
            continue
        root = str(link.get("href") or "").strip()
        if root:
            return urljoin(root.rstrip("/") + "/", "bricks/v1/")

    # At this point the page itself proved it is a Bricks query page. The REST
    # route below is the Bricks family contract, not a per-game or generated ID.
    parsed = urlparse(page_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/wp-json/bricks/v1/"
    raise ValueError("RubyPlay catálogo: no se pudo resolver el REST root Bricks.")


def parse_bricks_catalog_states(html: str, page_url: str) -> list[BricksCatalogState]:
    """Discover every Bricks query that publishes RubyPlay game posts.

    RubyPlay currently composes the archive from multiple independent
    ``post_type=games`` loops. They are all valid catalogue sources and may
    overlap, so choosing a single 'main' generated element is incorrect. The
    crawler must paginate each loop and deduplicate by game slug.
    """
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
            post_types = {str(value) for value in post_type}
        else:
            post_types = set()
        if "games" in post_types:
            trails.append((node, query_vars))

    if not trails:
        raise ValueError("RubyPlay catálogo: no se encontró query Bricks post_type=games.")

    bricks = _js_object_block(html, "bricksData")
    rest_api_url = _rest_api_url_from_page(html, page_url)
    nonce = _js_string(bricks, "nonce")
    wp_rest_nonce = _js_string(bricks, "wpRestNonce")
    post_id = _js_string(bricks, "postId")
    language = _js_string(bricks, "language") or "en"

    states: list[BricksCatalogState] = []
    seen_ids: set[str] = set()
    for node, query_vars in trails:
        query_element_id = str(
            node.get("data-query-element-id")
            or node.get("data-element-id")
            or ""
        ).strip()
        if not query_element_id or query_element_id in seen_ids:
            continue
        seen_ids.add(query_element_id)
        states.append(
            BricksCatalogState(
                rest_api_url=rest_api_url,
                nonce=nonce,
                wp_rest_nonce=wp_rest_nonce,
                post_id=post_id,
                language=language,
                query_element_id=query_element_id,
                query_vars=dict(query_vars),
                page=max(1, _int_value(node.get("data-page"), 1)),
                max_pages=max(1, _int_value(node.get("data-max-pages"), 1)),
                start=max(1, _int_value(node.get("data-start"), 1)),
                end=max(0, _int_value(node.get("data-end"), 0)),
                candidate_count=len(trails),
            )
        )

    if not states:
        raise ValueError("RubyPlay catálogo: queries games sin data-query-element-id.")
    return states


def parse_bricks_catalog_state(html: str, page_url: str) -> BricksCatalogState:
    """Compatibility helper for callers expecting exactly one game query."""
    states = parse_bricks_catalog_states(html, page_url)
    if len(states) != 1:
        raise ValueError(
            "RubyPlay catálogo: la página publica múltiples queries games; "
            "usar parse_bricks_catalog_states()."
        )
    return states[0]


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


def updated_query_element_id(payload: dict[str, Any]) -> str:
    raw = payload.get("updated_query")
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("element_id") or "").strip()
