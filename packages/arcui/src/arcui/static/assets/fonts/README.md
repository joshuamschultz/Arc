# Self-Hosted Fonts (SPEC-023 §FR-26 / NFR-9)

ArcUI runs in air-gapped environments. No served HTML or CSS may
reference an external font CDN. Two font families are bundled:

- **Inter** — UI body / headings, weights 400 / 500 / 600 / 700.
- **JetBrains Mono** — code blocks and inline `<code>`, weights 400 / 500.

Both ship under SIL Open Font License 1.1 (see `LICENSE-Inter.txt` and
`LICENSE-JetBrains-Mono.txt`).

## Layout

```
fonts/
├── README.md                       — this file
├── fonts.css                       — @font-face declarations
├── LICENSE-Inter.txt
├── LICENSE-JetBrains-Mono.txt
├── inter-regular.woff2             — Inter 400
├── inter-medium.woff2              — Inter 500
├── inter-semibold.woff2            — Inter 600
├── inter-bold.woff2                — Inter 700
├── jetbrains-mono-regular.woff2    — JetBrains Mono 400
└── jetbrains-mono-medium.woff2     — JetBrains Mono 500
```

## Where to download

- Inter: https://github.com/rsms/inter/releases (use Inter-X.YY.zip; copy
  the WOFF2 files renamed as above)
- JetBrains Mono: https://github.com/JetBrains/JetBrainsMono/releases
  (copy from the `webfonts/` subdirectory)

## Verifying

After dropping the WOFF2 files in this directory, run:

    pytest packages/arcui/tests/integration/test_self_hosted_fonts.py

The test asserts the served HTML contains no `fonts.googleapis.com`
reference and that each WOFF2 file is reachable at
`/assets/fonts/<name>.woff2`.
