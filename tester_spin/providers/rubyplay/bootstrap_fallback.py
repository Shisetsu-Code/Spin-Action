from __future__ import annotations

from pathlib import Path

from tester_spin.providers.rubyplay import runtime as _runtime
from tester_spin.providers.rubyplay.browser_launcher import capture_init_contract_browser


_ORIGINAL_BOOTSTRAP = _runtime.bootstrap_game


def _discover_profile_for_launcher(session, launcher, *, timeout_s: float):
    launcher_response = session.get(
        launcher.launcher_url,
        timeout=timeout_s,
        allow_redirects=True,
    )
    launcher_response.raise_for_status()
    launcher.provider_script_urls = _runtime.extract_provider_script_urls(
        launcher_response.text,
        launcher_response.url or launcher.launcher_url,
    )
    scripts: list[tuple[str, str]] = []
    for url in launcher.provider_script_urls:
        response = session.get(url, timeout=timeout_s)
        response.raise_for_status()
        scripts.append((url, response.text))
    return _runtime.discover_client_profile(scripts)


def bootstrap_game(
    session,
    public_url: str,
    *,
    timeout_s: float,
    cached_profile=None,
    artifact_dir: Path | None = None,
):
    """Use the official client envelope only when static init is rejected.

    Static bundle discovery remains the normal/fast path. If the gameserver
    explicitly rejects ``action=init``, Chromium opens the already-resolved final
    launcher and observes only v_protocol/v_math/device_type from the official
    client's first init request. Session keys/funModeData from Chromium are never
    imported; the tester immediately creates and owns a fresh demo session.
    """
    try:
        return _ORIGINAL_BOOTSTRAP(
            session,
            public_url,
            timeout_s=timeout_s,
            cached_profile=cached_profile,
            artifact_dir=artifact_dir,
        )
    except ValueError as exc:
        if "RubyPlay init: status=" not in str(exc):
            raise

        public = session.get(public_url, timeout=timeout_s, allow_redirects=True)
        public.raise_for_status()
        launcher_url = _runtime.extract_launcher_url(public.text, public.url or public_url)
        launcher = _runtime.parse_launcher_url(launcher_url)

        profile = _discover_profile_for_launcher(
            session,
            launcher,
            timeout_s=timeout_s,
        )
        observed = capture_init_contract_browser(
            launcher.launcher_url,
            timeout_s=max(15.0, timeout_s),
        )
        protocol = observed.get("v_protocol")
        math_version = observed.get("v_math")
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            raise ValueError(
                "RubyPlay init fallback: el cliente oficial no expuso v_protocol válido."
            ) from exc
        if not isinstance(math_version, int) or isinstance(math_version, bool):
            raise ValueError(
                "RubyPlay init fallback: el cliente oficial no expuso v_math válido."
            ) from exc

        profile.protocol_version = protocol
        profile.math_version = math_version
        profile.evidence.append("browser.official-init.v_protocol")
        profile.evidence.append("browser.official-init.v_math")

        fallback_dir = artifact_dir / "official-init-fallback" if artifact_dir else None
        return _ORIGINAL_BOOTSTRAP(
            session,
            public_url,
            timeout_s=timeout_s,
            cached_profile=profile,
            artifact_dir=fallback_dir,
        )


def install_bootstrap_fallback() -> None:
    from tester_spin.providers.rubyplay import execution as _execution

    _runtime.bootstrap_game = bootstrap_game
    _execution.bootstrap_game = bootstrap_game
