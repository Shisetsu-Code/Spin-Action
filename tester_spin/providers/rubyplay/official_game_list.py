from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from tester_spin.models import Game


_SHEET_PATH_RE = re.compile(r"^/spreadsheets/d/([A-Za-z0-9_-]+)/edit/?$")
_PUBLIC_GAME_PATH_RE = re.compile(r"^/games/([a-z0-9][a-z0-9-]*)/?$", re.I)
# The official RubyPlay Game List can contain studio namespaces beyond rp_
# (for example kg_). The authoritative identity is the exact Game ID matched
# against the official launcher gamename, not a hardcoded studio prefix.
_GAME_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]{0,31}_[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    re.I,
)
_REQUIRED_HEADERS = {"Name", "Status", "Release Date", "Game ID", "Demo Link"}


@dataclass(frozen=True, slots=True)
class OfficialGameListRecord:
    game: Game
    status: str
    release_date: str
    wager: str
    buy_feature: bool
    free_rounds: str = ""
    awarded_feature_support: str = ""
    default_rtp: str = ""
    theme: str = ""
    features: str = ""


def discover_game_list_sheet_url(html: str, base_url: str) -> str:
    """Find the single public Google Sheet linked by RubyPlay's Game List page."""
    del base_url
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        try:
            parsed = urlparse(href)
        except Exception:
            continue
        if parsed.scheme != "https" or parsed.netloc.casefold() != "docs.google.com":
            continue
        if not _SHEET_PATH_RE.fullmatch(parsed.path or ""):
            continue
        gid_values = [value for value in parse_qs(parsed.query).get("gid", []) if value]
        if len(set(gid_values)) != 1:
            continue
        canonical = urlunparse(("https", "docs.google.com", parsed.path, "", urlencode({"gid": gid_values[0]}), ""))
        if canonical not in candidates:
            candidates.append(canonical)
    if len(candidates) != 1:
        raise ValueError(
            "RubyPlay Game List: enlace Google Sheet no unívoco "
            f"(candidatos={len(candidates)})."
        )
    return candidates[0]


def build_sheet_csv_url(sheet_url: str) -> str:
    parsed = urlparse(str(sheet_url or ""))
    if parsed.scheme != "https" or parsed.netloc.casefold() != "docs.google.com":
        raise ValueError("RubyPlay Game List: host de hoja inesperado.")
    match = _SHEET_PATH_RE.fullmatch(parsed.path or "")
    if not match:
        raise ValueError("RubyPlay Game List: ruta de hoja inesperada.")
    gids = [value for value in parse_qs(parsed.query).get("gid", []) if value]
    if len(set(gids)) != 1:
        raise ValueError("RubyPlay Game List: gid no unívoco.")
    sheet_id = match.group(1)
    query = urlencode(
        {
            "tqx": "out:csv",
            "gid": gids[0],
            # The current official sheet has a deliberately blank column B and
            # extends through Demo Link near the right edge. A2:P truncated the
            # authoritative Demo Link column and forced a non-authoritative web
            # fallback. Keep enough width for the documented table through T.
            "range": "A2:T",
            "headers": "1",
        }
    )
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?{query}"


def _normalize_row(row: dict[str, str | None]) -> dict[str, str]:
    return {
        str(key or "").strip(): str(value or "").strip()
        for key, value in row.items()
        if str(key or "").strip()
    }


def _bounded_row_preview(rows: list[list[str]]) -> list[list[str]]:
    preview: list[list[str]] = []
    for row in rows[:2]:
        preview.append([str(cell or "").strip()[:80] for cell in row[:8]])
    return preview


def _header_index(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows[:20]):
        names = {str(cell or "").strip() for cell in row if str(cell or "").strip()}
        if _REQUIRED_HEADERS.issubset(names):
            return index
    raise ValueError(
        "RubyPlay Game List: encabezado oficial no encontrado; "
        f"primeras_filas={_bounded_row_preview(rows)!r}."
    )


def _validated_demo_identity(demo_url: str, game_id: str) -> tuple[str, str]:
    try:
        parsed = urlparse(demo_url)
    except Exception as exc:
        raise ValueError(f"RubyPlay Game List: Demo Link inválido para {game_id}.") from exc
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"RubyPlay Game List: Demo Link inválido para {game_id}.")

    host = parsed.hostname.casefold() if parsed.hostname else ""
    if host.startswith("www."):
        host = host[4:]
    public_match = _PUBLIC_GAME_PATH_RE.fullmatch(parsed.path or "")
    if host == "rubyplay.com" and public_match:
        slug = public_match.group(1).casefold()
        canonical = f"https://rubyplay.com/games/{slug}/"
        return slug, canonical

    if host == "demo.rubyplay.com" and (parsed.path or "").rstrip("/") == "/launcher":
        params = parse_qs(parsed.query, keep_blank_values=True)
        names = list(dict.fromkeys(str(item) for item in params.get("gamename", []) if str(item)))
        modes = list(dict.fromkeys(str(item) for item in params.get("mode", []) if str(item)))
        if names != [game_id] or modes != ["offline"]:
            raise ValueError(
                f"RubyPlay Game List: Demo Link no coincide con Game ID={game_id}."
            )
        return game_id.casefold(), demo_url

    raise ValueError(
        f"RubyPlay Game List: Demo Link fuera de hosts/rutas oficiales para {game_id}."
    )


def parse_official_game_list_csv(
    text: str,
    *,
    provider_key: str,
) -> list[OfficialGameListRecord]:
    """Parse the official sheet and return the complete set of released Active games."""
    raw_rows = list(csv.reader(io.StringIO(text or "")))
    if not raw_rows:
        raise ValueError("RubyPlay Game List: CSV vacío.")
    header_index = _header_index(raw_rows)
    payload = io.StringIO()
    writer = csv.writer(payload, lineterminator="\n")
    writer.writerows(raw_rows[header_index:])
    payload.seek(0)

    records: list[OfficialGameListRecord] = []
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for raw in csv.DictReader(payload):
        row = _normalize_row(raw)
        if not any(row.values()):
            continue
        status = row.get("Status", "")
        if status.casefold() != "active":
            continue
        name = row.get("Name", "")
        game_id = row.get("Game ID", "")
        demo_url = row.get("Demo Link", "")
        if not name:
            raise ValueError("RubyPlay Game List: fila Active con Name vacío.")
        if not _GAME_ID_RE.fullmatch(game_id):
            try:
                demo_parsed = urlparse(demo_url)
                demo_host = (demo_parsed.hostname or "").casefold()
                demo_path = str(demo_parsed.path or "")
                demo_query_keys = sorted(parse_qs(demo_parsed.query, keep_blank_values=True))
            except Exception:
                demo_host = ""
                demo_path = ""
                demo_query_keys = []
            raise ValueError(
                "RubyPlay Game List: Game ID inválido "
                f"{game_id!r} en {name!r}; "
                f"demo_host={demo_host!r}, demo_path={demo_path!r}, "
                f"demo_query_keys={demo_query_keys!r}."
            )
        canonical_id = game_id.casefold()
        if canonical_id in seen_ids:
            raise ValueError(f"RubyPlay Game List: Game ID duplicado: {game_id}.")
        if not demo_url:
            raise ValueError(f"RubyPlay Game List: Demo Link vacío para {game_id}.")
        slug, canonical_url = _validated_demo_identity(demo_url, canonical_id)
        if slug in seen_slugs:
            raise ValueError(f"RubyPlay Game List: slug duplicado: {slug}.")

        game = Game(
            provider=str(provider_key),
            slug=slug,
            name=name,
            url=canonical_url,
            symbol=canonical_id,
        )
        records.append(
            OfficialGameListRecord(
                game=game,
                status=status,
                release_date=row.get("Release Date", ""),
                wager=row.get("Wager", ""),
                buy_feature=row.get("Buy Feature", "").casefold() == "yes",
                free_rounds=row.get("Free Rounds", ""),
                awarded_feature_support=row.get("Awarded Feature Support", ""),
                default_rtp=row.get("Default RTP", ""),
                theme=row.get("Theme", ""),
                features=row.get("Features", ""),
            )
        )
        seen_ids.add(canonical_id)
        seen_slugs.add(slug)

    if not records:
        raise ValueError("RubyPlay Game List: no hay filas Active válidas.")
    return records


def is_official_game_list_target(game: Game) -> bool:
    """Accept both legacy public pages and direct official offline demo launchers."""
    if not str(game.name or "").strip():
        return False
    symbol = str(game.symbol or "").strip().casefold()
    if symbol and not _GAME_ID_RE.fullmatch(symbol):
        return False
    try:
        parsed = urlparse(str(game.url or ""))
    except Exception:
        return False
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if host.startswith("www."):
        host = host[4:]
    if host == "rubyplay.com" and _PUBLIC_GAME_PATH_RE.fullmatch(parsed.path or ""):
        return True
    if host != "demo.rubyplay.com" or (parsed.path or "").rstrip("/") != "/launcher":
        return False
    params = parse_qs(parsed.query, keep_blank_values=True)
    names = list(dict.fromkeys(str(item).casefold() for item in params.get("gamename", []) if str(item)))
    modes = list(dict.fromkeys(str(item).casefold() for item in params.get("mode", []) if str(item)))
    return bool(symbol and names == [symbol] and modes == ["offline"])
