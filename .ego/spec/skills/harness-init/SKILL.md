---
name: harness-init
description: "Initialize, inspect or reconfigure the ego installation in this project. Use only for harness setup, maintenance or diagnosing harness configuration."
---

<!-- EGO:PROMPT:BEGIN -->
# Harness initialization

Inspect the repository and call `ego init --root .` to preview configuration. Apply only with user approval via `ego init --root . --apply` or `ego sync --root . --apply`.

Run `ego doctor --root .` after applying. Do not overwrite user-owned files. Do not install external tools without informed consent. Prefer an existing CLI/MCP instead of adding redundant skills.
<!-- EGO:PROMPT:END -->
