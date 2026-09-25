# Vendored files

| File | Source | Version | License |
|---|---|---|---|
| `htmx.min.js` | npm `htmx.org` (`dist/htmx.min.js`), integrity checked against the registry | 2.0.11 | 0BSD |
| `fonts/figtree-latin-wght-normal.woff2` | npm `@fontsource-variable/figtree` (`files/`), integrity checked against the registry | 5.3.0 | OFL-1.1 |
| `fonts/fraunces-latin-wght-normal.woff2` | npm `@fontsource-variable/fraunces` (`files/`), integrity checked against the registry | 5.3.0 | OFL-1.1 |

The fonts are served from this server (no Google Fonts: no third-party requests, and the
CSP allows only `'self'`).

## three.js (only the "Box gestalten" page)

`vendor/three-0.186.1/`: npm `three` 0.186.1, tarball integrity checked against the registry
(MIT, `LICENSE` alongside). Unminified, because the package ships no minified module build
and there is no build step; nginx compresses it.

| File | Package path | sha256 of the original |
|---|---|---|
| `three.module.js` | `build/three.module.js` | `9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea` |
| `three.core.js` | `build/three.core.js` | `9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6` |
| `OrbitControls.js` | `examples/jsm/controls/OrbitControls.js` | `3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b` |

One change: `OrbitControls.js` imports `./three.module.js` instead of the bare specifier
`three`, since the CSP forbids the inline import map that would resolve it.
