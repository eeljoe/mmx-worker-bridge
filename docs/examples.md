# Examples

## Read-Only Inspection

```powershell
mmx-worker-bridge --task "Summarize the repository layout." --root "<project-root>"
```

## Patch Proposal

```powershell
mmx-worker-bridge --task "Propose a README fix without applying it." --root "<project-root>"
```

## Controlled Implementation

```powershell
mmx-worker-bridge create-worktree --root "<git-root>" --worktree-base "<worktree-dir>" --task-id "readme-fix"
mmx-worker-bridge --task "Fix README typos." --root "<worktree-path>" --allow-write --owns "README.md"
```

## Batch Dry Run

```json
[
  {
    "id": "docs",
    "task": "Inspect docs and propose updates.",
    "root": "<worktree-path>",
    "owns": ["docs"]
  }
]
```

```powershell
mmx-worker-bridge run-batch --tasks-file "tasks.json" --dry-run
```

