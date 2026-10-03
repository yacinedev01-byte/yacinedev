# Snablox PHP platform patches

These focused patches are for the PHP platform hosted separately from this Railway service repository. Verify the PHP site's files match each patch's base before applying. From the PHP site document root:

```sh
git apply --check site-patches/snablox-mobile-black-screen.patch
git apply site-patches/snablox-mobile-black-screen.patch
git apply --check site-patches/snablox-summary-icons.patch
git apply site-patches/snablox-summary-icons.patch
```

- `snablox-mobile-black-screen.patch` fixes mobile viewport expansion and keeps chat URL synchronization inside its application closure.
- `snablox-summary-icons.patch` uses the supplied circular SVG arrow and a custom high-contrast brain SVG. It also overrides the legacy global `svg[aria-hidden="true"]` sprite-hiding rule only for the visible Summary and thinking icons; without this scoped exception, those SVGs were positioned at `left:-9999px`.

No API keys or session tokens are included.