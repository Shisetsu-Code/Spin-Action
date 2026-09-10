from __future__ import annotations

import html as html_module
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from tester_spin.providers.rubyplay import runtime as _runtime
from tester_spin.providers.rubyplay.browser_launcher import (
    _pick_complete_launcher,
    resolve_demo_launcher_browser,
)


def _static_launcher_candidates(public_html: str, public_url: str) -> list[str]:
    """Collect launcher URLs without assuming a particular Bricks element.

    RubyPlay can expose a placeholder iframe plus the real demo target on another
    href/data-* attribute. We inspect URL-bearing attributes structurally and
    also recover absolute launcher URLs embedded in inline markup/scripts.
    """
    soup = BeautifulSoup(public_html or "", "html.parser")
    candidates: list[str] = []

    for node in soup.find_all(True):
        for _name, raw_value in node.attrs.items():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for raw in values:
                value = html_module.unescape(str(raw or "").strip())
                if not value or "/launcher" not in value:
                    continue
                try:
                    absolute = urljoin(public_url, value)
                except Exception:
                    continue
                if urlparse(absolute).path.rstrip("/").endswith("/launcher"):
                    candidates.append(absolute)

    for match in re.finditer(
        r"https?://[^\s'\"<>]+/launcher\?[^\s'\"<>]+",
        html_module.unescape(public_html or ""),
        re.I,
    ):
        candidates.append(match.group(0))

    return list(dict.fromkeys(candidates))


def extract_executable_launcher_url(public_html: str, public_url: str) -> str:
    """Return the final RubyPlay demo launcher, not the public-page placeholder."""
    candidates = _static_launcher_candidates(public_html, public_url)
    complete = _pick_complete_launcher(candidates)
    if complete:
        return complete

    # The static page sometimes publishes only a pre-demo placeholder (for
    # example gamename/server_url without currency). Follow the official Play
    # Demo interaction and capture the executable launcher it creates.
    return resolve_demo_launcher_browser(public_url, timeout_s=30.0)


def install_runtime_launcher_resolver() -> None:
    _runtime.extract_launcher_url = extract_executable_launcher_url
