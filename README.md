# mmx-worker-bridge

English | [中文](README.zh-CN.md)

MiniMax-powered external coding workers for Codex and Claude workflows.

`mmx-worker-bridge` wraps the MiniMax `mmx` CLI in a controlled worker loop. A lead agent can delegate a task, inspect artifacts, review proposed patches, and decide whether changes should be applied or merged.

![Architecture](docs/assets/architecture.svg)

## Why

Multi-provider credentials are awkward inside Claude Code and Codex CLI. You can build a relay service to aggregate API keys, but for MiniMax the simpler path is often better: let the authenticated `mmx` CLI own MiniMax credentials, then expose it as an external worker.

MiniMax is also a better fit for bounded sub-agent work than for the main development session. In practice, it can have a higher error rate on full autonomous development, but it has strong instruction-following behavior and benchmarks well enough to be useful for delegated, reviewable tasks.

This bridge is built around that role:

- Keep MiniMax credentials in `mmx`, not inside Claude Code or Codex.
- Run MiniMax as a bounded external worker, not the main session agent.
- Give the worker useful tools: file reads, search, directory listing, glob discovery, and constrained read-only commands.
- Produce reviewable artifacts: `result.md`, `run.jsonl`, `proposed_patches/`, `git diff`, and test output.
- Use Codex or Claude as the lead agent that reviews and decides what lands.

## Requirements

- Python 3.11 or newer.
- Git.
- The MiniMax `mmx` CLI installed and authenticated.

```powershell
mmx auth
```

## Install From Source

```powershell
git clone <repo-url> mmx-worker-bridge
cd mmx-worker-bridge
C:/Python313/python.exe -m pip install -e .
```

## Quickstart

Run a read-only worker:

```powershell
mmx-worker-bridge --task "Inspect this repository and summarize the test strategy." --root "<project-root>" --out-dir "mmx-subagent-runs"
```

Review a proposed patch:

```powershell
mmx-worker-bridge review-patch --root "<project-root>" --patch "<patch.diff>"
```

Create an isolated worktree for implementation:

```powershell
mmx-worker-bridge create-worktree --root "<git-root>" --worktree-base "<worktree-dir>" --task-id "docs-update"
```

Run controlled write mode in that worktree:

```powershell
mmx-worker-bridge --task "Update docs for the new option." --root "<worktree-path>" --allow-write --owns "docs"
```

Run a batch dry-run before parallel execution:

```powershell
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --dry-run
```

## Documentation

- [Codex guide](docs/codex.md)
- [Claude guide](docs/claude.md)
- [Safety model](docs/safety.md)
- [Examples](docs/examples.md)
- [Agent-readable install guide](Install.md)

## Safety

![Review gate](docs/assets/review-gate.svg)

`run_command` is not arbitrary shell access. It rejects shell syntax, write-oriented commands, and root-external paths. File changes go through patch artifacts or controlled `apply_patch` in an isolated worktree.

## Status

Early extraction from a local prototype. APIs may change before the first stable release.
