# Snablox PHP platform patches

These focused patches are for the PHP platform hosted separately from this Railway service repository. Verify the PHP site's files match each patch's base before applying. From the PHP site document root:

```sh
git apply --check site-patches/snablox-mobile-black-screen.patch
git apply site-patches/snablox-mobile-black-screen.patch
git apply --check site-patches/snablox-summary-icons.patch
git apply site-patches/snablox-summary-icons.patch
```

- `snablox-mobile-black-screen.patch` fixes mobile viewport expansion and keeps chat URL synchronization inside its application closure.
- `snablox-summary-icons.patch` uses the supplied Android Vector path for the thinking icon and the circular SVG arrow. It renders only the actual `thinking → tool → thinking → tool` sequence, captures the agent's real post-tool thought, and suppresses replayed SSE events by step ID so retries/new steps remain visible without duplicated rows. It also scopes an exception to the legacy global `svg[aria-hidden="true"]` sprite-hiding rule; without it, visible Summary icons were positioned at `left:-9999px`.

The Summary list retains every event produced during a run; this does not disable the agent's existing execution limits. No API keys or session tokens are included.