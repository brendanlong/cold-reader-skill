# Claude Code Instructions

## Versioning

When making changes to plugin content (SKILL.md, scripts, personas, plugin.json, etc.),
bump the `version` field in `.claude-plugin/marketplace.json`. This is required for
`claude plugin update` to detect changes and re-fetch the cache. Use semver: bump the minor
version for new features or structural changes, patch for fixes.

## Testing the chunker

`plugins/cold-reader/skills/cold-reader/scripts/chunk.py` is a self-contained `uv` script.
Run it directly (the shebang invokes `uv run`):

```bash
./plugins/cold-reader/skills/cold-reader/scripts/chunk.py SOURCE.md --workdir /tmp/out
```
