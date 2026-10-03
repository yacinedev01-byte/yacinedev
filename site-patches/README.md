# Snablox PHP platform patches

These focused patches are for the PHP platform hosted separately from this Railway service repository. Verify the PHP site's files match each patch's base before applying. From the PHP site document root:

```sh
git apply --check site-patches/snablox-mobile-black-screen.patch
git apply site-patches/snablox-mobile-black-screen.patch
git apply --check site-patches/snablox-summary-icons.patch
git apply site-patches/snablox-summary-icons.patch
```

- `snablox-mobile-black-screen.patch` fixes mobile viewport expansion and keeps chat URL synchronization inside its application closure.
- `snablox-summary-icons.patch` uses the supplied Android Vector path for the thinking icon and the circular SVG arrow. It renders the actual `thinking → tool → thinking → tool` sequence, displays streamed thought text in the same row as its icon, and compacts an adjacent empty thinking row into the following real thought row. Replayed SSE events are suppressed by step ID so retries/new steps remain visible without duplicate cards. A scoped CSS exception restores the visible icons hidden by the legacy global `svg[aria-hidden="true"]` sprite rule.

The Summary list retains every event produced during a run; this does not disable the agent's existing execution limits. No API keys or session tokens are included.