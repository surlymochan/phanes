### Fixed

- Let the language checker scan examples outside the OpenBot workspace with repeated `--example-root` options. Keep required paths and unused translation checks strict when the legacy examples directory is absent.
- Scan the optional sibling examples directory from the default language check command. Test default calls, standalone checkouts, and unused keys through the real CLI.
