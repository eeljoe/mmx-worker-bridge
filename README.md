# mmx-worker-bridge

English | [中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-24%20passed-brightgreen?logo=pytest)](tests/)
[![Code Style](https://img.shields.io/badge/code%20style-dataclass-blue)](https://docs.python.org/3/library/dataclasses.html)

**Sandboxed multi-agent orchestration bridge** — delegates bounded coding tasks to MiniMax as an external worker, with lead-agent review gates, git worktree isolation, and patch-based code review.

![Architecture](docs/assets/architecture.svg)

---

## Abstract

Modern AI coding assistants (Claude Code, Codex CLI) excel as lead agents but face challenges with multi-provider credential management and autonomous execution reliability. `mmx-worker-bridge` addresses this by wrapping the MiniMax `mmx` CLI as a **sandboxed external worker** within a lead-agent orchestration pattern.

The system implements a **review-gate architecture**: the worker operates in a root-scoped sandbox with constrained tools (file reads, search, glob discovery, read-only commands), produces reviewable artifacts (`result.md`, `run.jsonl`, `proposed_patches/`), and only applies changes through a lead-agent-approved patch workflow. Write operations require explicit git worktree isolation, path ownership declarations, and `git apply --check` validation.

**Key insight**: MiniMax demonstrates strong instruction-following for bounded sub-agent tasks, making it more suitable as a delegated worker than a main session agent. This bridge leverages that strength while maintaining human-in-the-loop review at every write boundary.

---

## Features

| Category | Capability |
|----------|-----------|
| **Sandboxed Execution** | Root-scoped file operations, shell injection prevention, path traversal protection |
| **Review Gate** | All patches require lead-agent approval before application |
| **Git Worktree Isolation** | Write operations occur in isolated branches, never the main worktree |
| **Path Ownership** | Batch tasks declare owned paths; overlapping ownership is rejected |
| **Agentic Tool Loop** | Multi-step tool-use loop with `read_file`, `rg_search`, `list_dir`, `glob_find`, `run_command`, `propose_patch`, `apply_patch`, `final_answer` |
| **Batch Execution** | Parallel task execution with conflict detection and dry-run validation |
| **Retry with Backoff** | Exponential backoff for transient `mmx` CLI failures |
| **Artifact Protocol** | Structured outputs: `result.md`, `run.jsonl`, `proposed_patches/*.diff`, `batch.summary.json` |

---

## How It Works

### Agentic Loop

```
┌─────────────────────────────────────────────────────────────┐
│  Lead Agent (Claude / Codex)                                │
│  ├─ Delegates bounded task                                  │
│  ├─ Reviews artifacts (result.md, patches, git diff)        │
│  └─ Decides: apply / reject / request changes               │
└──────────────────────────────┬──────────────────────────────┘
                               │ task prompt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  mmx-worker-bridge                                          │
│  ├─ Constructs system prompt with tool schemas              │
│  ├─ Invokes mmx text chat --messages-file                   │
│  ├─ Parses tool_use responses                               │
│  ├─ Executes tools in sandbox (ReadOnlyTools)               │
│  ├─ Feeds tool_result back to mmx                           │
│  └─ Loops until final_answer or max_steps                   │
└──────────────────────────────┬──────────────────────────────┘
                               │ mmx CLI
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  MiniMax API (credentials managed by mmx auth)              │
└─────────────────────────────────────────────────────────────┘
```

### Write Boundary

| Mode | Tools Available | Write Access |
|------|----------------|--------------|
| **Default (read-only)** | `read_file`, `rg_search`, `list_dir`, `glob_find`, `run_command`, `propose_patch`, `final_answer` | None — patches saved as artifacts |
| **Controlled write** | All above + `apply_patch` | Only in git worktree, only for `--owns` paths, only after `git apply --check` passes |

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
│   └── test_bridge.py           # 24 tests covering security, patches, batch, worktree
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
└── README.md                    # This file
```

---

## Technical Highlights

- **Zero external dependencies** — pure Python 3.11+ stdlib (`dataclasses`, `subprocess`, `concurrent.futures`, `pathlib`)
- **Protocol-oriented design** — `CompletionClient` protocol enables mock testing and alternative backends
- **Security-first tool sandbox** — `run_command` rejects shell metacharacters (`&|;<>`\`()`), root-external paths, and write-oriented commands
- **Patch validation pipeline** — unified diff parsing → root scope check → ownership check → `git apply --check` → `git apply`
- **Batch ownership conflict detection** — normalizes paths and detects overlaps before execution
- **Windows-compatible** — handles `.CMD`/`.BAT` shims via `PATHEXT` resolution

---

## Requirements

- Python 3.11 or newer
- Git
- The MiniMax `mmx` CLI installed and authenticated

```powershell
mmx auth
```

## Installation

```powershell
git clone git@github.com:eeljoe/mmx-worker-bridge.git
cd mmx-worker-bridge
pip install -e .
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

`mmx-worker-bridge` enforces a **lead-agent review gate** for all write operations:

1. **Default mode is read-only** — `run_command` rejects shell syntax, write commands, and root-external paths
2. **Patches are artifacts** — `propose_patch` saves diffs for review, never applies directly
3. **Write mode requires isolation** — `apply_patch` only works in git worktrees with `--allow-write` and `--owns` declarations
4. **Every patch is validated** — unified diff format → root scope → ownership → `git apply --check` → `git apply`

The worker cannot modify files outside its declared scope. The lead agent (Claude, Codex, or human) inspects all artifacts before merging.

---

## Testing

```powershell
pip install pytest
pytest tests/
```

Test coverage includes:
- Root scope enforcement (path traversal rejection)
- Shell injection prevention
- Patch validation and ownership checks
- Batch ownership conflict detection
- Git worktree isolation
- Retry with exponential backoff
- Tool loop execution with mock clients

---

## Status

Early extraction from a local prototype. APIs may change before the first stable release.

---

## License

MIT © 2026 mmx-worker-bridge contributors
