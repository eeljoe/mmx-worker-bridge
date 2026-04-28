# Claude Workflow

Claude can call `mmx-worker-bridge` as an external worker from a terminal workflow. This avoids pushing MiniMax credentials into Claude Code: the authenticated `mmx` CLI owns the credentials, while Claude remains the lead agent that reviews worker artifacts.

This project does not claim native Claude subagent integration. The bridge is intentionally plain CLI so Claude, Codex, and other lead agents can share the same artifact protocol.

## Recommended Flow

1. Ask Claude to create or select a worktree.
2. Ask Claude to run `mmx-worker-bridge` for a bounded task.
3. Ask Claude to inspect the worker artifacts and `git diff`.
4. Apply patches only after review.

## Example

```powershell
mmx-worker-bridge --task "Inspect the failing tests and propose a patch." --root "<worktree-path>" --out-dir "mmx-subagent-runs"
```

See `examples/claude-workflow/AGENTS.md` for an example policy block.
