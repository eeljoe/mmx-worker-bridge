# mmx-worker-bridge

[English](README_en.md) | [中文](README.md)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-31%20passed-brightgreen?logo=pytest)](tests/)
[![Code Style](https://img.shields.io/badge/code%20style-dataclass-blue)](https://docs.python.org/3/library/dataclasses.html)

**Sandboxed multi-agent orchestration bridge** — delegates bounded coding tasks to MiniMax as an external worker, with lead-agent review gates, git worktree isolation, and patch-based code review.

![Architecture](docs/assets/architecture.svg)

---

## ⚠️ Project Background

> **Note**: With **Pi Agent** now available, the practical value of this project has decreased.
>
> This was built when MiniMax charged by **API call count**. Calls were abundant, making MiniMax suitable as an external worker for batch tasks — code cleanup, batch refactoring, repetitive work. The "lead agent + external worker" pattern was reasonable at the time.
>
> Things have changed:
> 1. **Pi Agent** is now available — building multi-agent systems is faster and better
> 2. **MiniMax switched to token-based billing** — the "more calls = cheaper" advantage is gone
> 3. In practice, using Pi Agent directly gives much better results
>
> **Why keep this around**:
> - Some design patterns are worth referencing: path sandboxing, patch validation, ownership conflict detection
> - Understanding how API billing models (per-call vs per-token) affect architecture choices
> - Learning case for multi-agent orchestration
>
> If you're building something similar today, consider using Pi Agent or other modern multi-agent frameworks.

---

## What This Is

This wraps MiniMax's `mmx` CLI as a **sandboxed external worker**.

Claude Code and Codex CLI work great as lead agents, but multi-provider credential management is troublesome and fully autonomous AI execution has risks. This bridge solves that:

- Worker runs in a limited directory scope with restricted tools (read files, search, read-only commands)
- Each operation generates reviewable artifacts (`result.md`, `run.jsonl`, `proposed_patches/`)
- File modifications require a patch workflow — lead agent reviews before applying
- Write operations must happen in git worktree, declare owned paths, and pass `git apply --check`

**Core idea**: MiniMax is reliable for bounded sub-tasks with good instruction following, making it suitable as a delegated worker rather than a main session agent. This leverages that while maintaining review at every write boundary.

---

## Features

| Feature | Description |
|---------|-------------|
| **Sandboxed Execution** | File ops limited to specified directory, prevents shell injection and path traversal |
| **Review Gate** | All code changes need lead agent approval before applying |
| **Git Worktree Isolation** | Write ops in separate branches, won't affect main worktree |
| **Path Ownership** | Batch tasks declare owned paths, conflicts cause errors |
| **Tool Loop** | Multi-step ops: read files, search, list dirs, run commands, propose patches, return results |
| **Batch Execution** | Parallel tasks with conflict detection and dry-run |
| **Retry with Backoff** | Auto-retry on mmx failures with exponential backoff |
| **Artifact Output** | Generates `result.md`, `run.jsonl`, patch files, etc. |

---

## How It Works

### Agent Loop

```
┌─────────────────────────────────────────────────────────────┐
│  Lead Agent (Claude / Codex)                                │
│  ├─ Delegates task to worker                                │
│  ├─ Reviews artifacts (result.md, patches, git diff)        │
│  └─ Decides: apply / reject / request changes               │
└──────────────────────────────┬──────────────────────────────┘
                               │ task prompt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  mmx-worker-bridge                                          │
│  ├─ Builds system prompt with available tools               │
│  ├─ Calls mmx text chat                                     │
│  ├─ Parses tool_use responses                               │
│  ├─ Executes tools in sandbox                               │
│  ├─ Feeds results back to mmx                               │
│  └─ Loops until final_answer or max steps                   │
└──────────────────────────────┬──────────────────────────────┘
                               │ mmx CLI
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  MiniMax API (credentials managed by mmx auth)              │
└─────────────────────────────────────────────────────────────┘
```

### Write Permissions

| Mode | Available Tools | Write Access |
|------|----------------|--------------|
| **Default (read-only)** | read files, search, list dirs, read-only commands, propose patches, return results | None — patches saved as artifacts |
| **Controlled write** | All above + apply_patch | Git worktree only, declared paths only, requires `git apply --check` pass |

---

## Project Structure

```
mmx-worker-bridge/
├── src/
│   └── mmx_worker_bridge/
│       ├── __init__.py          # Public API exports
│       ├── cli.py               # CLI entry point
│       ├── core.py              # Worker loop, tools, batch, worktree (~1400 LOC)
│       ├── client.py            # mmx CLI client wrapper
│       ├── worker.py            # MmxWorker re-exports
│       ├── batch.py             # Batch execution re-exports
│       ├── worktree.py          # Git worktree isolation re-exports
│       ├── patch_review.py      # Patch review re-exports
│       └── tools.py             # Tool schema definitions
├── tests/
│   └── test_bridge.py           # 31 tests covering security, patches, batch, worktree
├── docs/
│   ├── assets/                  # Architecture diagrams (SVG)
│   ├── safety.md                # Safety model documentation
│   ├── claude.md                # Claude Code integration guide
│   ├── codex.md                 # Codex CLI integration guide
│   ├── examples.md              # Usage examples
│   └── release.md               # Release process
├── examples/
│   ├── claude-workflow/         # AGENTS.md policy block example
│   └── codex-skill/             # Codex skill configuration
├── pyproject.toml               # PEP 621 project metadata
├── LICENSE                      # MIT License
└── README_en.md                 # This file
```

---

## Technical Details

- **Zero external dependencies** — pure Python 3.11+ stdlib, just used `dataclasses`, `subprocess`, `concurrent.futures`, `pathlib` and stuff
- **Protocol-oriented design** — `CompletionClient` is a Protocol, easy to mock for testing, can swap backends
- **Security-first** — `run_command` rejects shell metacharacters (`&|;<>`\`()`), paths outside root, and write-oriented commands
- **Patch validation flow** — parse unified diff → check if in root → check ownership → `git apply --check` → actually apply
- **Batch conflict detection** — checks for path ownership overlaps before running
- **Windows compatible** — handles `.CMD`/`.BAT` shims via `PATHEXT` resolution

---

## Installation

### Quick Install (For Agents)

Copy this to your agent:

```
Clone and install mmx-worker-bridge from https://github.com/eeljoe/mmx-worker-bridge, then follow the Install.md instructions to set it up.
```

Or the detailed version:

```
1. git clone https://github.com/eeljoe/mmx-worker-bridge.git
2. cd mmx-worker-bridge
3. pip install -e .
4. Make sure mmx CLI is installed and authenticated (mmx auth)
5. See Install.md for detailed configuration
```

### Manual Installation

```powershell
git clone git@github.com:eeljoe/mmx-worker-bridge.git
cd mmx-worker-bridge
pip install -e .
```

### Prerequisites

- Python 3.11+
- Git
- MiniMax `mmx` CLI installed and authenticated

```powershell
mmx auth
```

## Quickstart

### Read-Only Inspection

```powershell
mmx-worker-bridge --task "Inspect this repository and summarize the test strategy." --root "<project-root>"
```

### Patch Proposal (No Write)

```powershell
mmx-worker-bridge --task "Propose a README fix without applying it." --root "<project-root>"
```

### Review a Proposed Patch

```powershell
mmx-worker-bridge review-patch --root "<project-root>" --patch "<patch.diff>"
```

### Controlled Implementation (Git Worktree)

```powershell
# 1. Create isolated worktree
mmx-worker-bridge create-worktree --root "<git-root>" --worktree-base "<worktree-dir>" --task-id "docs-update"

# 2. Run worker with write access in worktree
mmx-worker-bridge --task "Update docs for the new option." --root "<worktree-path>" --allow-write --owns "docs"
```

### Batch Execution

```powershell
# Dry-run to check ownership conflicts
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --dry-run

# Execute with parallelism
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --parallel 2
```

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Codex Integration](docs/codex.md) | Use mmx-worker-bridge as a Codex skill |
| [Claude Workflow](docs/claude.md) | Lead-agent workflow with Claude Code |
| [Safety Model](docs/safety.md) | Sandbox, review gates, write boundaries |
| [Examples](docs/examples.md) | Common usage patterns |
| [Agent Install Guide](Install.md) | Machine-readable installation instructions |

---

## Safety

![Review Gate](docs/assets/review-gate.svg)

`mmx-worker-bridge` enforces **lead agent review** for all write operations:

1. **Default is read-only** — `run_command` rejects shell syntax, write commands, paths outside root
2. **Patches are just artifacts** — `propose_patch` just saves the diff, doesn't apply directly
3. **Write needs isolation** — `apply_patch` only works in git worktree, needs `--allow-write` and `--owns` declarations
4. **Every patch gets validated** — unified diff format → root scope → ownership → `git apply --check` → then actually apply

Worker can't modify files outside its declared scope. Lead agent (Claude, Codex, or human) checks all artifacts before merging.

---

## Testing

```powershell
pip install pytest
pytest tests/
```

31 tests covering:
- Root scope enforcement (path traversal gets rejected)
- Shell injection prevention
- Patch validation and ownership checks
- Batch ownership conflict detection
- Git worktree isolation
- Retry with exponential backoff
- Tool loop execution with mock clients

---

## Status

Just extracted from local prototype. API might still change.

---

## License

MIT © 2026 mmx-worker-bridge contributors
