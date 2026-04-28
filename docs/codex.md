# Codex Integration

Codex can use `mmx-worker-bridge` as an external worker CLI. This avoids putting MiniMax credentials into Codex itself: the authenticated `mmx` CLI owns the credentials, while Codex remains the lead agent that reviews worker artifacts.

This is not native `spawn_agent`; it is a controlled subprocess workflow for bounded, reviewable MiniMax worker tasks.

## Recommended Flow

1. Create or choose a git worktree for implementation tasks.
2. Run a read-only worker first.
3. For writes, pass `--allow-write` only inside the worktree and always declare `--owns`.
4. Inspect `result.md`, `run.jsonl`, `proposed_patches/`, and `git diff`.
5. Run project tests before merging.

## Example

```powershell
mmx-worker-bridge --task "Review docs and propose improvements." --root "<worktree-path>" --out-dir "mmx-subagent-runs"
```

## Codex Rule Snippet

See `examples/codex-skill/SKILL.md` for a skill-style integration example.
