from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from tester_spin.models import Game


@dataclass(slots=True)
class BGamingCatalogRecord:
    game: Game
    public_url: str
    demo_url: str
    rtp: float | None = None
    volatility: str = ""
    game_type: str = ""
    availability: str = "DEMO"


def _text(node) -> str:
    return "" if node is None else " ".join(node.get_text(" ", strip=True).split())


def _identifier_from_demo_url(url: str) -> str:
    if not url:
        return ""
    parts = [part for part in urlparse(url).path.split("/") if part]
    for marker in ("play", "games"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1].strip()
    return ""


def _slug_from_detail_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "games" in parts:
        idx = parts.index("games")
        if idx + 1 < len(parts):
            return parts[idx + 1].strip().lower()
    return ""


def _rtp(card) -> float | None:
    labels = card.select(".bottom_container > div")
    for block in labels:
        if "rtp" not in _text(block).casefold():
            continue
        for node in block.select(".paragraph-102"):
            value = _text(node).replace("%", "").strip()
            try:
                return float(value)
            except ValueError:
                continue
    return None


def parse_catalog_html(html: str) -> list[BGamingCatalogRecord]:
    soup = BeautifulSoup(html or "", "html.parser")
    records: list[BGamingCatalogRecord] = []
    seen: set[str] = set()

    for card in soup.select("[data-catalog-card]"):
        detail = card.select_one('a[href*="bgaming.com/games/"]')
        public_url = str(detail.get("href") or "").strip() if detail else ""
        slug = _slug_from_detail_url(public_url)
        if not slug or slug in seen:
            continue

        name = ""
        image = card.select_one("img[alt]")
        if image:
            name = str(image.get("alt") or "").strip()
        if not name:
            name = _text(card.select_one(".heading-35"))
        if not name:
            continue

        demo_url = ""
        for anchor in card.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if "bgaming-network.com" not in href:
                continue
            if "/play/" in href or "/games/" in href:
                demo_url = href
                break

        identifier = _identifier_from_demo_url(demo_url)
        demo_has_ephemeral_token = False
        if demo_url:
            query = parse_qs(urlparse(demo_url).query)
            demo_has_ephemeral_token = any(
                key.casefold() in {"play_token", "launch_token", "token"}
                for key in query
            )

        thumbnail = str(card.get("data-image") or "").strip()
        if not thumbnail and image:
            thumbnail = str(image.get("src") or "").strip()

        volatility = ""
        volatility_node = card.select_one(".paragraph-98")
        if volatility_node:
            volatility = _text(volatility_node)

        game_type = _text(card.select_one(".game-type-text"))
        card_text = _text(card).casefold()
        if demo_has_ephemeral_token:
            availability = "EPHEMERAL_DEMO"
            safe_game_url = public_url
            safe_demo_url = ""
        elif demo_url:
            availability = "DEMO"
            safe_game_url = demo_url
            safe_demo_url = demo_url
        elif "coming soon" in card_text:
            availability = "COMING_SOON"
            safe_game_url = public_url
            safe_demo_url = ""
        else:
            availability = "NO_DEMO"
            safe_game_url = public_url
            safe_demo_url = ""

        records.append(
            BGamingCatalogRecord(
                game=Game(
                    provider="bgaming",
                    slug=slug,
                    name=name,
                    url=safe_game_url,
                    thumbnail_url=thumbnail,
                    symbol=identifier,
                ),
                public_url=public_url,
                demo_url=safe_demo_url,
                rtp=_rtp(card),
                volatility=volatility,
                game_type=game_type,
                availability=availability,
            )
        )
        seen.add(slug)

    return records
