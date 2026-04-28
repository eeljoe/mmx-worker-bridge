# Security

`mmx-worker-bridge` is designed around a lead-agent review gate.

## Boundaries

- Workers do not get arbitrary shell access.
- `run_command` is constrained to read-only allowlisted commands.
- Default mode stores patch artifacts instead of changing files.
- Write mode requires an isolated git worktree and declared owned paths.
- Applying or merging changes remains a lead-agent decision.

## Reporting Issues

Do not include API keys, private prompts, or private repository contents in issue reports. Open a minimal report with reproduction steps and expected behavior.

## Threat Model

Assume worker model output is untrusted. The bridge validates paths, patch headers, command syntax, and ownership declarations before tool results affect the filesystem. Lead agents should still inspect artifacts and run tests before merging worker output.
