# Snablox PHP platform patches

These focused patches are for the PHP platform hosted separately from this Railway service repository. Verify the PHP site's files match each patch's base before applying. From the PHP site document root:

```sh
git apply --check site-patches/snablox-mobile-black-screen.patch
git apply site-patches/snablox-mobile-black-screen.patch
git apply --check site-patches/snablox-summary-icons.patch
git apply site-patches/snablox-summary-icons.patch
```

- `snablox-mobile-black-screen.patch` fixes mobile viewport expansion and keeps chat URL synchronization inside its application closure.
- `snablox-summary-icons.patch` replaces the Summary row's text chevron with the supplied circular SVG arrow and adds an original SVG brain/thinking icon.

No API keys or session tokens are included.