from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse

from tester_spin.models import Game


@dataclass(slots=True)
class RedTigerCatalogRecord:
    game: Game
    cms_id: int | str | None
    table_id: str
    game_type: str
    provider_name: str
    release_date: str
    has_bonus_buy: bool | None
    raw_attributes: dict[str, Any]
    launch_id: str = ""


def _attributes(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    attrs = item.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _media_url(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    data = value.get("data")
    attrs = _attributes(data)
    return str(attrs.get("url") or "").strip()


def _wp_media_url(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("url") or "").strip()


def _rendered_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered")
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _wp_term_names(item: dict[str, Any], taxonomy: str) -> list[str]:
    embedded = item.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    groups = embedded.get("wp:term")
    if not isinstance(groups, list):
        return []
    found: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if not isinstance(term, dict):
                continue
            if str(term.get("taxonomy") or "") != taxonomy:
                continue
            name = _rendered_text(term.get("name"))
            if name and name not in found:
                found.append(name)
    return found


def provider_id_from_catalog_url(catalog_url: str) -> str:
    """Read the public game-provider filter from the official Evolution URL.

    The numeric provider id is deliberately not duplicated as a source constant;
    the public catalog URL supplied by Evolution remains the authority for it.
    """
    parsed = urlparse(str(catalog_url or ""))
    values: list[str] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in {"game_provider[]", "game_provider[0]", "game_provider"}:
            clean = str(value or "").strip()
            if clean:
                values.append(clean)
    unique = list(dict.fromkeys(values))
    if len(unique) != 1:
        raise ValueError(
            "Evolution Games: el URL del catálogo debe contener un único filtro game_provider."
        )
    return unique[0]


def wp_catalog_query_params(
    provider_id: str,
    *,
    page: int,
    page_size: int = 36,
    custom_sort: str = "featured",
) -> dict[str, Any]:
    """Exact public WordPress catalog shape observed in the Evolution HAR."""
    return {
        "_embed": 1,
        "acf_format": "standard",
        "page": max(1, int(page)),
        "per_page": max(1, min(100, int(page_size))),
        "game_provider[]": str(provider_id),
        "custom_sort": str(custom_sort or "featured"),
        "only_games": 1,
    }


def parse_wp_game_record(item: Any) -> RedTigerCatalogRecord | None:
    """Parse one public games.evolution.com WordPress game record.

    ``id`` is the launch authority used by /wp-json/games/v1/start. ``acf.game_id``
    is kept only as catalog metadata because the captured catalog contains duplicate
    values for distinct titles, so it must not be used as the launch identity.
    """
    if not isinstance(item, dict):
        return None
    post_id = item.get("id")
    slug = str(item.get("slug") or "").strip().strip("/")
    name = _rendered_text(item.get("title"))
    public_url = str(item.get("link") or "").strip()
    acf = item.get("acf") if isinstance(item.get("acf"), dict) else {}
    if post_id is None or not slug or not name or not public_url:
        return None

    parsed_public = urlparse(public_url)
    if parsed_public.scheme not in {"http", "https"} or not parsed_public.netloc:
        return None

    provider = acf.get("game_provider")
    provider_name = ""
    if isinstance(provider, dict):
        provider_name = _rendered_text(provider.get("post_title"))

    game_types = _wp_term_names(item, "game_type")
    features = [value.casefold() for value in _wp_term_names(item, "game_feature")]
    has_bonus_buy: bool | None = None
    if any("bonus buy" in value or "feature buy" in value for value in features):
        has_bonus_buy = True

    launch_id = str(post_id).strip()
    catalog_game_id = str(acf.get("game_id") or "").strip()
    thumbnail_url = _wp_media_url(acf.get("game_thumbnail"))
    game = Game(
        provider="redtiger",
        slug=slug,
        name=name,
        url=public_url,
        thumbnail_url=thumbnail_url,
        symbol=launch_id,
    )
    return RedTigerCatalogRecord(
        game=game,
        cms_id=post_id,
        table_id=catalog_game_id,
        game_type=(game_types[0] if game_types else ""),
        provider_name=provider_name,
        release_date=str(acf.get("release_year") or item.get("date") or ""),
        has_bonus_buy=has_bonus_buy,
        raw_attributes=acf,
        launch_id=launch_id,
    )


def parse_wp_games_page(payload: Any) -> list[RedTigerCatalogRecord]:
    if not isinstance(payload, list):
        raise ValueError("Evolution Games catálogo: respuesta WordPress inválida.")
    return [
        record
        for record in (parse_wp_game_record(item) for item in payload)
        if record is not None
    ]


# Legacy cmsevo parsing helpers are retained for historical artifacts/tests only.
# The active provider catalog now uses games.evolution.com.
def find_studio_id(payload: Any, studio_name: str) -> int | str:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("CMS studios: respuesta inválida.")
    target = " ".join(str(studio_name or "").split()).casefold()
    matches: list[int | str] = []
    for item in payload["data"]:
        attrs = _attributes(item)
        title = " ".join(str(attrs.get("title") or "").split()).casefold()
        if title == target and isinstance(item, dict) and item.get("id") is not None:
            matches.append(item["id"])
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        raise ValueError(
            f"CMS studios: se esperaba un único {studio_name!r}; encontrados={unique!r}."
        )
    return unique[0]


def parse_game_record(item: Any, *, public_catalog_url: str) -> RedTigerCatalogRecord | None:
    if not isinstance(item, dict):
        return None
    attrs = _attributes(item)
    name = str(attrs.get("name") or "").strip()
    slug = str(attrs.get("slug") or "").strip().strip("/")
    table_id = str(attrs.get("tableId") or "").strip()
    if not name or not slug or not table_id:
        return None

    game_type = str(attrs.get("gameType") or "").strip()
    provider_name = str(attrs.get("provider") or "").strip()
    origin = f"{urlparse(public_catalog_url).scheme}://{urlparse(public_catalog_url).netloc}/"
    public_url = urljoin(origin, f"games/{slug}")
    thumbnail_url = _media_url(attrs.get("icon"))
    game = Game(
        provider="redtiger",
        slug=slug,
        name=name,
        url=public_url,
        thumbnail_url=thumbnail_url,
        symbol=table_id,
    )
    return RedTigerCatalogRecord(
        game=game,
        cms_id=item.get("id"),
        table_id=table_id,
        game_type=game_type,
        provider_name=provider_name,
        release_date=str(attrs.get("releaseDate") or ""),
        has_bonus_buy=(attrs.get("hasBonusBuy") if isinstance(attrs.get("hasBonusBuy"), bool) else None),
        raw_attributes=attrs,
    )


def parse_games_page(payload: Any, *, public_catalog_url: str) -> tuple[list[RedTigerCatalogRecord], dict[str, int]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("CMS games: respuesta inválida.")
    records = [
        record
        for record in (
            parse_game_record(item, public_catalog_url=public_catalog_url)
            for item in payload["data"]
        )
        if record is not None
    ]

    pagination: dict[str, int] = {}
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("pagination"), dict):
        for key in ("page", "pageSize", "pageCount", "total"):
            value = meta["pagination"].get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                pagination[key] = value
    return records, pagination


def games_query_params(studio_id: int | str, *, page: int, page_size: int, released_before_iso: str) -> dict[str, Any]:
    return {
        "filters[studio][$eq]": studio_id,
        "filters[releaseDate][$lte]": released_before_iso,
        "populate": "deep",
        "sort[0]": "releaseDate:desc",
        "pagination[pageSize]": max(1, int(page_size)),
        "pagination[page]": max(1, int(page)),
    }
