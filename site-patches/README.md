# Snablox PHP platform patches

These focused patches are for the PHP platform hosted separately from this Railway service repository. Apply them from the PHP site document root and only when the files match the expected base version:

```sh
git apply --check site-patches/snablox-mobile-black-screen.patch
git apply site-patches/snablox-mobile-black-screen.patch

git apply --check site-patches/snablox-summary-icons.patch
git apply site-patches/snablox-summary-icons.patch

git apply --check site-patches/snablox-railway-bridge.patch
git apply site-patches/snablox-railway-bridge.patch
```

- `snablox-mobile-black-screen.patch` fixes mobile viewport expansion and keeps chat URL synchronization inside its application closure.
- `snablox-summary-icons.patch` uses the supplied Android Vector path for the thinking icon and the circular SVG arrow. It renders the actual `thinking → tool → thinking → tool` sequence, shows streamed thought text beside its icon, merges an adjacent empty thinking row into the following real thought, and suppresses replayed SSE events by step ID. It also restores icons hidden by the legacy global SVG rule, reduces Summary icon/container sizes, and slightly thins the timeline rail.
- `snablox-railway-bridge.patch` pins PHP cURL requests to HTTP/1.1, parses the JSON response even when the host appends HTML after it, and adds a versioned polling asset to avoid stale static-file caches.

The deployment's private `config/secrets.php` must point `shell_api_url` at the currently deployed Railway service origin. Keep `shell_api_key`, session tokens, and the entire secrets file private; they are intentionally excluded from this repository and all patches. The deployment's stale Railway hostname has been corrected without changing its API key. Job-step limits remain unchanged; the Summary display does not disable them.
