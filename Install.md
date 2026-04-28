# Install mmx-worker-bridge

This file is intended for coding agents such as Codex CLI and Claude Code.

If a user says:

```text
Help me clone mmx-worker-bridge if needed, then set it up for Codex and Claude worker use by following https://raw.githubusercontent.com/<owner>/mmx-worker-bridge/main/Install.md
```

read this file and complete the steps below.

## Goal

Install `mmx-worker-bridge` locally and configure the current coding-agent environment so it can delegate bounded tasks to a MiniMax-powered external worker.

The installation should provide:

1. The `mmx-worker-bridge` Python package and CLI.
2. Verification that the `mmx` CLI exists and has usable credentials.
3. A Codex skill when Codex skill directories are available.
4. A Claude Code integration through `CLAUDE.md` or `AGENTS.md`, and a skill-style file when the user's Claude setup has a known skills directory.
5. Verification output showing the wrapper is usable.

## Rules

- Be idempotent. If files already exist, update only the managed block or report that no change is needed.
- Do not print or store API keys.
- Do not overwrite user-authored rules outside clearly marked managed blocks.
- Prefer editable install from the cloned repository.
- Use PowerShell on Windows and POSIX shell elsewhere.
- Install and authenticate `mmx` before installing or configuring the worker bridge.
- If `mmx` is not installed or not authenticated, stop with clear next steps instead of guessing.

## Inputs

Use these defaults unless the user provides different values:

- Repository URL: `https://github.com/<owner>/mmx-worker-bridge.git`
- Clone directory: a user workspace directory named `mmx-worker-bridge`
- Python: `python` or the active Python interpreter
- Codex home candidates: `$CODEX_HOME`, then `~/.codex`
- Claude home candidates: `$CLAUDE_HOME`, then `~/.claude`
- Claude project rule files: `AGENTS.md`, then `CLAUDE.md`

If the repository URL still contains `<owner>`, ask the user for the real GitHub URL before cloning.

## Step 1: Locate Or Clone

If the current directory is already the `mmx-worker-bridge` repository, use it.

Otherwise, check whether `mmx-worker-bridge` already exists under the current workspace. If it exists, enter it. If not, clone the repository URL the user provided.

Example:

```powershell
git clone "https://github.com/<owner>/mmx-worker-bridge.git" "mmx-worker-bridge"
cd "mmx-worker-bridge"
```

## Step 2: Check Git And Python

Run:

```powershell
git --version
python --version
```

If either command is missing, stop and report the missing prerequisite.

## Step 3: Check And Authenticate mmx CLI

First check whether `mmx` exists:

```powershell
mmx --help
```

If `mmx` is missing, stop and tell the user to install the MiniMax CLI first. Do not continue to configure the worker bridge without `mmx`, because the bridge delegates model calls through that CLI.

Then check credential status:

```powershell
mmx auth status --output json --non-interactive
```

If this succeeds and reports an authenticated account or usable API-key state, continue.

If it fails, or reports that the user is not authenticated, ask the user to run one of:

```powershell
mmx auth login
```

or, if the installed CLI aliases login through `auth`:

```powershell
mmx auth
```

After the user completes authentication, re-run:

```powershell
mmx auth status --output json --non-interactive
```

Do not ask for the API key in chat. Do not print any token or API key returned by the CLI.

## Step 4: Install Package

Install editable:

```powershell
python -m pip install -e .
```

Verify:

```powershell
mmx-worker-bridge --help
mmx-worker-bridge run-batch --help
```

## Step 5: Install Codex Skill

If Codex home is available, create:

```text
<codex-home>/skills/mmx-worker-bridge/SKILL.md
```

Use `examples/codex-skill/SKILL.md` from this repository as the source. If the file already exists, replace only if it appears to be the same managed skill or ask before overwriting.

After installation, report the skill path.

## Step 6: Install Claude Code Integration

Claude Code may not use the same skill directory convention as Codex in every environment. Configure it in this order:

1. If the user has an explicit Claude skills directory, create `mmx-worker-bridge/SKILL.md` there using `examples/codex-skill/SKILL.md` as the source.
2. Otherwise, update the project-level `CLAUDE.md` or `AGENTS.md` managed block in Step 7.
3. If neither a skills directory nor a project rule file is available, print the managed block and report that Claude integration needs user review.

Do not guess private Claude configuration paths beyond `$CLAUDE_HOME` and `~/.claude`.

## Step 7: Add Project Policy Block

If the current target project has `AGENTS.md`, append or update this managed block.

If it has `CLAUDE.md` instead, append or update the same block there.

If neither exists, create `AGENTS.md` only when the user asked for project-local configuration. Otherwise, print the block for the user to review.

Managed block:

```markdown
<!-- mmx-worker-bridge:start -->
## mmx-worker-bridge

- Use `mmx-worker-bridge` when the user asks for MiniMax/mmx as a coding worker, sub-agent, patch proposer, or delegated implementer.
- This is an external worker CLI, not a native Codex or Claude subagent.
- Use read-only mode first.
- For implementation tasks, use an isolated git worktree plus `--allow-write --owns "<path>"`.
- The `mmx` CLI itself does not directly edit project files. File modification is provided by this bridge through `propose_patch` and controlled `apply_patch`.
- Review `result.md`, `run.jsonl`, `proposed_patches/`, `git diff`, and tests before applying or merging worker output.
- Never expose arbitrary shell access to the worker. `run_command` is constrained to read-only allowlisted commands.
<!-- mmx-worker-bridge:end -->
```

## Step 8: Verify Worker Write Capability Boundary

The MiniMax `mmx` CLI is a model/API CLI. It can call tools when a wrapper provides tool schemas, but it does not edit files by itself.

This bridge provides the file-modification toolset:

- Default mode exposes `propose_patch`, which saves a unified diff artifact for review.
- Controlled write mode exposes `apply_patch` only when the user passes `--allow-write`.
- `apply_patch` requires declared owned paths through `--owns`.
- Recommended implementation runs happen inside an isolated git worktree.

Verify the help text includes write-control flags:

```powershell
mmx-worker-bridge --help
```

Expected: the help output includes `--allow-write` and `--owns`.

## Step 9: Verify Safety

Run the test suite if this is a development checkout:

```powershell
python -m pytest -q
```

Run a quick CLI check:

```powershell
mmx-worker-bridge --help
```

If a git project is available, optionally run a dry batch validation with a tiny sample `tasks.json`.

## Success Report

When done, report:

- Repository path.
- `mmx --help` status.
- `mmx auth status` status without printing secrets.
- Package install status.
- `mmx-worker-bridge --help` status.
- Codex skill path, or why it was skipped.
- Claude Code integration path, or why it was skipped.
- Project policy file updated, or why it was skipped.
- Any user action still required, especially MiniMax CLI install or `mmx auth login`.

## Failure Report

If blocked, report:

- The command that failed.
- The relevant error text.
- The next concrete action for the user.
