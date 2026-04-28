---
name: mmx-worker-bridge
description: Use when delegating bounded coding work to MiniMax through mmx-worker-bridge.
---

# mmx-worker-bridge

Use `mmx-worker-bridge` as an external worker CLI. This is not Codex native `spawn_agent`.

## Rules

- Use read-only mode first.
- Use `--allow-write` only inside an isolated git worktree.
- Always pass `--owns` for write tasks.
- Review `result.md`, `run.jsonl`, `proposed_patches/`, `git diff`, and tests before merging.
