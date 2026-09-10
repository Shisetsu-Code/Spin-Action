# BGaming automatic HAR capture

Tester-Spin prepares reusable browser HARs for BGaming before each real suite test.
This is a provider-specific diagnostic artifact step; other providers keep their own
execution/capture policies.

## Reuse rule

Before opening a browser, BGaming recursively searches the game's local data folder
for any non-empty `*.har` file. If one exists, capture is skipped completely: no demo
resolution, HTTP session, or Chromium process is started for HAR preparation.

The automatically generated path is:

```text
data/providers/bgaming/<game>/analysis/browser.har
```

A manually supplied HAR anywhere below the same game folder is also accepted. Files
named `*.partial.har` and zero-byte HARs are never considered reusable.

`data/` is ignored by Git, so raw HARs and ephemeral demo credentials remain local.

## GUI: abrir la carpeta HAR

La vista de detalle incluye `Abrir carpeta HAR` junto a las acciones existentes del
juego. El botón consulta al provider mediante `har_artifact_dir(game)`; la GUI no
conoce rutas internas de BGaming.

- Si existe un HAR, abre exactamente el directorio que contiene ese archivo.
- Si la captura falló pero existe `analysis/` con diagnósticos, abre `analysis/`.
- Si todavía no existe HAR ni diagnóstico, informa que hay que ejecutar el juego una
  vez para preparar los artefactos.

En Windows se usa Explorer; macOS y Linux usan sus abridores de directorios nativos.

## Automatic capture

When no HAR exists, the provider resolves a current demo URL and Playwright launches
Chromium headless with a full HAR recorder:

```text
record_har_mode=full
record_har_content=embed
service_workers=block
```

The browser loads the actual demo, waits for bootstrap, attempts generic entry
controls (`Play`, `Start`, `Continue`, etc.), finds visible canvases across frames,
focuses the game surface, and presses Space to try to trigger one ordinary demo spin.
There are no game-name, slug, or identifier selectors.

Even when the keyboard interaction does not trigger a spin, the HAR still contains
the browser bootstrap, `window.__OPTIONS__`, init traffic, loaders, bundles, scripts,
and other network evidence needed for protocol analysis.

The HAR is first written as `browser.partial.har`. It becomes `browser.har` only after
Playwright closes the browser context and produces a non-empty file. Failed captures
remove the partial file so it cannot suppress a future retry.

## Persistent diagnostics

Every BGaming game also maintains:

```text
data/providers/bgaming/<game>/analysis/har-debug.jsonl
```

This file is append-only JSON Lines and survives after the GUI closes. It combines
preparation/capture evidence and runner progress so a failed game can be diagnosed
without reproducing the entire session immediately.

Important capture stages include:

```text
prepare_start
demo_resolution_start / ok / failed
capture_start
playwright_start
browser_launch_start / ok
browser_context_ok
navigation_start / ok
bootstrap_wait_complete
entry_controls_scan / entry_control_clicked
spin_probe_start
spin_probe_canvas / spin_probe_fallback
spin_probe_success / spin_probe_exhausted
provider_post_request / provider_post_response
network_request_failed
console
page_error
context_close_start / ok
capture_complete
har_promoted
prepare_complete
```

Runner stages include:

```text
runner_start
runner_progress
runner_result
runner_exception
```

`runner_progress` mirrors the useful BGaming execution messages already shown in the
GUI: bootstrap, runtime family/profile, init, discovered modes, purchase probes,
HTTP/RPC failures, continuations, balances and final validation state.

Provider POST diagnostics recognize both API-v2 `/api/...` traffic and HyperHive
`/hyperhive` JSON-RPC traffic. For each request the debug file stores only structural
information such as:

```text
command / method / jsonrpc
top-level keys
option_keys
extra_data_keys
params_keys
req_keys
HTTP response status
sanitized URL
```

It deliberately does **not** write token values, bet values, purchase values or raw
request bodies into `har-debug.jsonl`. URLs are sanitized before persistence. The raw
HAR remains the complete local source of truth when those values are needed for
protocol analysis.

## Metadata

`analysis/har-capture.json` records a sanitized summary:

```text
captured_at
sanitized launch_url
bytes
provider_post_requests
elapsed_ms
interaction
debug_log
```

The raw HAR itself is intentionally not sanitized because request/response bodies and
loaded JavaScript are the diagnostic source of truth. It remains under ignored local
`data/`.

## Failure semantics

HAR preparation is best-effort. A Playwright/browser/demo-resolution failure is logged
but never converts an otherwise valid provider test into `ERROR`. The scheduler still
runs `test_game()` afterward.

Stopping the suite during preparation returns the game as cancelled instead of
starting another protocol test.
