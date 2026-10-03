# Snablox PHP platform patch

`snablox-mobile-black-screen.patch` contains the focused mobile-layout and JavaScript fixes prepared for the PHP platform hosted separately from this Railway service repository.

From the PHP site's document root, after verifying the deployed files match the patch's base revision, apply with:

```sh
git apply --check snablox-mobile-black-screen.patch
git apply snablox-mobile-black-screen.patch
```

The patch moves the fixed Summary sheet to `document.body`, clips horizontal overflow that expands the mobile layout viewport, removes the duplicate `think_ui.js` include, and keeps chat-URL synchronization within its application closure. No API keys or session tokens are included.
