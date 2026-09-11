from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

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


def find_studio_id(payload: Any, studio_name: str) -> int | str:
    """Find the CMS studio structurally by its advertised title.

    Numeric IDs are deployment data and are deliberately not part of the provider
    contract. If the CMS changes the Red Tiger studio ID, discovery still works.
    """
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
