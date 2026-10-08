# Public baseline and step 0

The raw handoff is ignored under `docs/tmp/`. Public files were copied to `docs/handoff/`, excluding `local-only/` and merged full handoff documents. Root `.gitignore` derives from `PUBLICATION_EXCLUSIONS.example`, additionally excluding `docs/tmp/` and the pre-existing private `docs/server-baseline.md`. That inventory remains intact locally. Root `.env.example` keeps every variable blank; `.env` contains an empty MODEL_API_KEY and is ignored. Root planning and packages/contracts are live copies.

The first handoff validation failed because the original manifest and six Markdown links referenced the intentionally excluded private annex. In the public copy only, those links became explicit private-annex-omitted text and the manifest was regenerated. No private placeholder was added. Raw source handoff remains unchanged. This is publication normalization, not a product-specification change.

The requested grep command was executed. `--exclude-dir=docs/tmp` does not exclude a nested directory in GNU/BSD grep; raw private matches are expected. Publication verification therefore scans `git ls-files --cached --others --exclude-standard -z` and validates candidate files, then staged files before a future commit. The string used in CODEX_START_PROMPT's scanner example is not a host address.

No staging, commit or push has been performed.
