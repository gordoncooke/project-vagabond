# Application archives

Each archive contains the captured application files at its archive root. `install.sh` unpacks each archive into that application's destination.

| Archive | Captured source | Local deployment changes |
|---|---|---|
| `cyberchef-v11.4.0.tar.gz` | CyberChef 11.4.0 standalone release | Versioned HTML copied to `index.html` |
| `omnitools-v0.6.0-89a50e3.tar.gz` | OmniTools commit `89a50e3` | Vite base path, router basename, and locale path set for `/tools/omnitools/` |
| `excalidraw-c0ad61c6.tar.gz` | Excalidraw commit `c0ad61c6` build | Base path, asset path, and fonts set for local `/tools/excalidraw/` use |
| `minipaint-v4.14.3.tar.gz` | Captured miniPaint files; bundled `package.json` reports version 4.14.3 | No local changes are documented; original Pi install source/revision is unknown |
| `flatnotes-v5.5.5.tar.gz` | Captured FlatNotes 5.5.5 source and built client | Runtime paths and service configuration are installed separately |
| `protomaps-font-glyphs-reference-pi.tar.gz` | Font glyph assets captured from the reference Pi; notes record a shallow clone of [`protomaps/basemaps-assets`](https://github.com/protomaps/basemaps-assets) on September 14, with no commit recorded | Unpacks under `/srv/library/maps/assets/fonts`; contains four Noto Sans glyph families and the OFL license |

These are installer payloads, not build sources. Recreate an archive from a reviewed extracted build using a gzip-compressed tar archive whose entries start at the application root. Preserve the local path/font changes listed above when rebuilding OmniTools or Excalidraw.

The installer also downloads EmulatorJS 4.2.3 from its versioned upstream 7z release. It clones the Lafvin UPS monitor at commit `9db38f1` and the Lafvin e-paper driver at commit `126a4bd`; the installer applies the recorded GPIO9 reset-pin change. The notes do not record the historical commits, so these pins make future runs repeatable but may differ from the original Pi checkouts.
