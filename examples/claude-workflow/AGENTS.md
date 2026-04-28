# Claude Workflow Example

Use `mmx-worker-bridge` when a bounded task should be delegated to MiniMax.

- Do not treat MMX as a native Claude subagent.
- Run workers through the CLI.
- Keep write tasks inside isolated git worktrees.
- Require `--owns` for controlled write mode.
- Review artifacts and tests before applying or merging changes.
