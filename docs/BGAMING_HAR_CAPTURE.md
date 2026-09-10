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

## Metadata

`analysis/har-capture.json` records a sanitized summary:

```text
captured_at
sanitized launch_url
bytes
provider_post_requests
elapsed_ms
interaction
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
