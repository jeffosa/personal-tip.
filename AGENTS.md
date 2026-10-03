# AGENTS.md — prop_alpha

This file exists for tools that read `AGENTS.md`. It deliberately holds no rules of its own, so it cannot
drift from the real instructions.

Before reading or changing anything in this repository, read and follow, in this order of precedence:

1. **`AGENT.md`**: the full code-modification protocol. It overrides everything else.
2. **`CLAUDE.md`**: current status, commands, conventions and the done-checklist. It is the single
   up-to-date project guide for every assistant, not only Claude.

The current test baseline is `EXPECTED_TESTS` in `ci/bootstrap.sh`. Never edit `.env` or print its values.
