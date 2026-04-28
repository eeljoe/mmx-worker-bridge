# Safety Model

`mmx-worker-bridge` is built around a lead-agent review gate.

## Worker Tools

- `read_file`: root-scoped UTF-8 file reads with line numbers.
- `rg_search`: root-scoped text search.
- `list_dir`: root-scoped directory listing.
- `glob_find`: root-scoped file discovery; absolute and parent traversal patterns are rejected.
- `run_command`: constrained read-only command execution with `shell=False`.
- `propose_patch`: save patch artifacts for review.
- `apply_patch`: controlled write mode with ownership and git checks.
- `final_answer`: finish the worker loop.

## Write Boundary

Default mode does not write project files. Implementation mode requires:

- An isolated git worktree.
- `--allow-write`.
- One or more `--owns` paths.
- A patch that passes validation and `git apply --check`.

## Can mmx Modify Files?

The `mmx` CLI itself does not directly modify project files. It is a model/API CLI. File modification happens only because `mmx-worker-bridge` provides tool schemas and executes tool calls on the worker's behalf.

The bridge exposes two modification paths:

- `propose_patch` in default mode saves a unified diff artifact for review.
- `apply_patch` in controlled write mode applies a unified diff only when `--allow-write` and matching `--owns` paths are set.

`run_command` remains read-only and is not a write-capable shell.

## Lead-Agent Gate

Codex, Claude, or another lead agent should inspect artifacts, diffs, and tests before applying or merging changes.
