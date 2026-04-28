from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_LINES = 200
DEFAULT_MAX_RESULTS = 50
DEFAULT_MAX_PATCH_BYTES = 100_000
DEFAULT_MAX_PATCH_FILES = 20
DEFAULT_STABLE_TOOL_DIR = Path.home() / ".local" / "share" / "mmx-worker-bridge"
SHELL_METACHAR_PATTERN = re.compile(r"[&|;<>`()\r\n]")


@dataclass
class WorkerConfig:
    root: Path
    out_dir: Path
    max_steps: int = 12
    max_tokens: int = 1200
    timeout: int = 120
    temperature: float = 0.1
    model: str | None = None
    mmx_bin: str = "mmx"
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_retries: int = 4
    retry_base_seconds: float = 1.0
    allow_write: bool = False
    owned_paths: list[str] | None = None


@dataclass
class RunResult:
    text: str
    run_dir: Path
    result_path: Path
    messages_path: Path
    run_log_path: Path


@dataclass
class PatchReviewResult:
    status: str
    root: Path
    patch_path: Path
    files: list[str]
    git_apply_check: dict[str, str]
    applied: bool


@dataclass
class InstallResult:
    status: str
    source_dir: Path
    target_dir: Path
    manifest_path: Path
    files: list[str]


@dataclass
class BatchTask:
    id: str
    task: str
    root: Path
    max_steps: int = 12
    owns: list[str] | None = None
    allow_write: bool = False


@dataclass
class BatchItemResult:
    task_id: str
    status: str
    text: str
    run_dir: Path | None
    error: str | None = None


@dataclass
class BatchRunResult:
    status: str
    out_dir: Path
    summary_path: Path
    results: list[BatchItemResult]


@dataclass
class WorktreeResult:
    status: str
    repo_root: Path
    path: Path
    branch: str


class CompletionClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_paths: list[Path],
        system_prompt: str,
    ) -> dict[str, Any]:
        ...


class ReadOnlyTools:
    def __init__(
        self,
        root: Path,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        patch_dir: Path | None = None,
        max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES,
        max_patch_files: int = DEFAULT_MAX_PATCH_FILES,
        allow_write: bool = False,
        owned_paths: list[str] | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.max_file_bytes = max_file_bytes
        self.patch_dir = Path(patch_dir).expanduser().resolve() if patch_dir else None
        self.max_patch_bytes = max_patch_bytes
        self.max_patch_files = max_patch_files
        self.allow_write = allow_write
        self.owned_paths = [_normalize_owned_path(path) for path in owned_paths or []]
        self.patch_count = 0

    def read_file(self, tool_input: dict[str, Any]) -> str:
        path_text = str(tool_input.get("path", "")).strip()
        if not path_text:
            return "ERROR: read_file requires a non-empty path."

        resolved = self._resolve_under_root(path_text)
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"ERROR: file not found: {resolved}"
        if not resolved.is_file():
            return f"ERROR: not a file: {resolved}"
        if resolved.stat().st_size > self.max_file_bytes:
            return f"ERROR: file too large: {resolved}"

        start_line = _positive_int(tool_input.get("start_line"), 1)
        max_lines = _positive_int(tool_input.get("max_lines"), DEFAULT_MAX_LINES)
        max_lines = min(max_lines, DEFAULT_MAX_LINES)

        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        start_index = start_line - 1
        selected = lines[start_index : start_index + max_lines]
        if not selected:
            return f"{resolved}: no lines in requested range."

        return "\n".join(
            f"{resolved}:{start_index + offset + 1}: {line}"
            for offset, line in enumerate(selected)
        )

    def rg_search(self, tool_input: dict[str, Any]) -> str:
        pattern = str(tool_input.get("pattern", "")).strip()
        if not pattern:
            return "ERROR: rg_search requires a non-empty pattern."

        path_text = str(tool_input.get("path") or ".").strip()
        resolved = self._resolve_under_root(path_text)
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"ERROR: search path not found: {resolved}"

        glob_pattern = tool_input.get("glob")
        if glob_pattern is not None:
            glob_pattern = str(glob_pattern).strip() or None
        max_results = min(
            _positive_int(tool_input.get("max_results"), DEFAULT_MAX_RESULTS),
            DEFAULT_MAX_RESULTS,
        )

        rg_result = self._rg_search_with_binary(
            pattern=pattern,
            search_path=resolved,
            glob_pattern=glob_pattern,
            max_results=max_results,
        )
        if rg_result is not None:
            return rg_result

        return self._rg_search_with_python(
            pattern=pattern,
            search_path=resolved,
            glob_pattern=glob_pattern,
            max_results=max_results,
        )

    def final_answer(self, tool_input: dict[str, Any]) -> str:
        status = str(tool_input.get("status") or "done").strip()
        summary = str(tool_input.get("summary") or "").strip()
        evidence = tool_input.get("evidence") or []

        lines = [f"Status: {status}", ""]
        lines.append(summary or "No summary provided.")
        if isinstance(evidence, list) and evidence:
            lines.extend(["", "Evidence:"])
            lines.extend(f"- {item}" for item in evidence)
        return "\n".join(lines).strip()

    def list_dir(self, tool_input: dict[str, Any]) -> str:
        path_text = str(tool_input.get("path") or ".").strip()
        resolved = self._resolve_under_root(path_text)
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"ERROR: path not found: {resolved}"
        if not resolved.is_dir():
            return f"ERROR: not a directory: {resolved}"

        recursive = bool(tool_input.get("recursive", False))
        max_depth = min(_positive_int(tool_input.get("max_depth"), 3), 5)
        max_entries = min(_positive_int(tool_input.get("max_entries"), 200), 500)

        entries: list[str] = []
        count = 0

        def _walk(current: Path, depth: int) -> None:
            nonlocal count
            if count >= max_entries or depth > max_depth:
                return
            try:
                items = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except PermissionError:
                entries.append(f"{current}/ <permission denied>")
                return
            for item in items:
                if count >= max_entries:
                    entries.append(f"... (truncated at {max_entries} entries)")
                    return
                rel = item.relative_to(resolved)
                prefix = "  " * depth
                if item.is_dir():
                    entries.append(f"{prefix}{rel}/")
                    count += 1
                    if recursive:
                        _walk(item, depth + 1)
                else:
                    try:
                        size = item.stat().st_size
                        entries.append(f"{prefix}{rel}  ({size:,} bytes)")
                    except OSError:
                        entries.append(f"{prefix}{rel}")
                    count += 1

        _walk(resolved, 0)
        if not entries:
            return f"{resolved}: empty directory."
        return "\n".join(entries)

    def glob_find(self, tool_input: dict[str, Any]) -> str:
        pattern = str(tool_input.get("pattern") or "").strip()
        if not pattern:
            return "ERROR: glob_find requires a non-empty pattern."
        pattern_error = _validate_glob_pattern(pattern)
        if pattern_error:
            return f"ERROR: {pattern_error}"

        path_text = str(tool_input.get("path") or ".").strip()
        resolved = self._resolve_under_root(path_text)
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"ERROR: search path not found: {resolved}"

        max_results = min(_positive_int(tool_input.get("max_results"), 50), 200)

        matches = sorted(resolved.glob(pattern))[:max_results]
        if not matches:
            return f"No matches for glob '{pattern}' under {resolved}."

        lines = []
        for m in matches:
            resolved_match = m.resolve(strict=False)
            try:
                resolved_match.relative_to(self.root)
            except ValueError:
                return f"ERROR: glob match outside root: {resolved_match}"
            rel = m.relative_to(resolved)
            if m.is_dir():
                lines.append(f"{rel}/")
            else:
                try:
                    size = m.stat().st_size
                    lines.append(f"{rel}  ({size:,} bytes)")
                except OSError:
                    lines.append(f"{rel}")
        return "\n".join(lines)

    def run_command(self, tool_input: dict[str, Any]) -> str:
        command = str(tool_input.get("command") or "").strip()
        if not command:
            return "ERROR: run_command requires a non-empty command."

        args, error = _parse_read_only_command(command, self.root)
        if error:
            return f"ERROR: {error}"

        timeout_seconds = min(_positive_int(tool_input.get("timeout"), 15), 60)
        work_dir = str(self.root)

        internal_result = self._run_internal_command(args)
        if internal_result is not None:
            return internal_result

        try:
            completed = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                timeout=timeout_seconds,
                cwd=work_dir,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout_seconds}s."
        except FileNotFoundError:
            return f"ERROR: command executable not found: {args[0]}"

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        max_output = 5000
        if len(stdout) > max_output:
            stdout = stdout[:max_output] + f"\n... (truncated, {len(stdout)} total chars)"
        if len(stderr) > max_output:
            stderr = stderr[:max_output] + f"\n... (truncated, {len(stderr)} total chars)"

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if completed.returncode != 0:
            parts.append(f"EXIT CODE: {completed.returncode}")
        return "\n".join(parts) if parts else "(no output)"

    def _run_internal_command(self, args: list[str]) -> str | None:
        command = _command_name(args[0])
        if command == "echo":
            return " ".join(args[1:])
        if command not in {"cat", "type"}:
            return None
        if len(args) < 2:
            return f"ERROR: {command} requires at least one file path."

        contents = []
        for raw_path in args[1:]:
            if raw_path.startswith("-"):
                return f"ERROR: unsupported {command} option: {raw_path}"
            resolved = self._resolve_under_root(raw_path)
            if isinstance(resolved, str):
                return resolved
            if not resolved.exists():
                return f"ERROR: file not found: {resolved}"
            if not resolved.is_file():
                return f"ERROR: not a file: {resolved}"
            if resolved.stat().st_size > self.max_file_bytes:
                return f"ERROR: file too large: {resolved}"
            contents.append(resolved.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(text.rstrip("\n") for text in contents).strip()

    def propose_patch(self, tool_input: dict[str, Any]) -> str:
        if self.patch_dir is None:
            return "ERROR: propose_patch is not configured for this run."

        patch = str(tool_input.get("patch") or tool_input.get("diff") or "")
        summary = str(tool_input.get("summary") or "").strip()
        if not patch.endswith("\n"):
            patch += "\n"

        files, error = self._validate_unified_patch(patch)
        if error:
            return f"ERROR: {error}"

        self.patch_count += 1
        patch_id = f"patch-{self.patch_count:03d}"
        self.patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self.patch_dir / f"{patch_id}.diff"
        metadata_path = self.patch_dir / f"{patch_id}.json"
        patch_path.write_text(patch, encoding="utf-8")

        metadata = {
            "status": "saved",
            "patch_id": patch_id,
            "summary": summary,
            "files": files,
            "patch_path": str(patch_path),
            "metadata_path": str(metadata_path),
            "git_apply_check": self._git_apply_check(patch_path),
        }
        _write_json(metadata_path, metadata)
        return json.dumps(metadata, ensure_ascii=False)

    def apply_patch(self, tool_input: dict[str, Any]) -> str:
        if not self.allow_write:
            return "ERROR: apply_patch is not enabled for this worker run."
        if self.patch_dir is None:
            return "ERROR: apply_patch is not configured for this run."

        patch = str(tool_input.get("patch") or tool_input.get("diff") or "")
        summary = str(tool_input.get("summary") or "").strip()
        if not patch.endswith("\n"):
            patch += "\n"

        files, error = self._validate_unified_patch(patch)
        if error:
            return f"ERROR: {error}"
        ownership_error = self._validate_patch_ownership(files)
        if ownership_error:
            return f"ERROR: {ownership_error}"

        self.patch_count += 1
        patch_id = f"patch-{self.patch_count:03d}"
        self.patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self.patch_dir / f"{patch_id}.diff"
        metadata_path = self.patch_dir / f"{patch_id}.json"
        patch_path.write_text(patch, encoding="utf-8")

        check = self._git_apply_check(patch_path)
        if check.get("status") != "passed":
            reason = check.get("detail") or check.get("reason") or "git apply check did not pass"
            return f"ERROR: cannot apply patch: {reason}"

        completed = subprocess.run(
            ["git", "-C", str(self.root), "apply", str(patch_path)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            return f"ERROR: git apply failed: {_truncate(detail, 2000)}"

        metadata = {
            "status": "applied",
            "patch_id": patch_id,
            "summary": summary,
            "files": files,
            "patch_path": str(patch_path),
            "metadata_path": str(metadata_path),
            "git_apply_check": check,
        }
        _write_json(metadata_path, metadata)
        return json.dumps(metadata, ensure_ascii=False)

    def execute(self, name: str, tool_input: dict[str, Any]) -> str:
        if name == "read_file":
            return self.read_file(tool_input)
        if name == "rg_search":
            return self.rg_search(tool_input)
        if name == "list_dir":
            return self.list_dir(tool_input)
        if name == "glob_find":
            return self.glob_find(tool_input)
        if name == "run_command":
            return self.run_command(tool_input)
        if name == "propose_patch":
            return self.propose_patch(tool_input)
        if name == "apply_patch":
            return self.apply_patch(tool_input)
        if name == "final_answer":
            return self.final_answer(tool_input)
        return f"ERROR: unknown tool: {name}"

    def _resolve_under_root(self, value: str) -> Path | str:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return f"ERROR: path outside root: {resolved}"
        return resolved

    def _validate_unified_patch(self, patch: str) -> tuple[list[str], str | None]:
        if not patch.strip():
            return [], "propose_patch requires a non-empty unified diff."
        if len(patch.encode("utf-8")) > self.max_patch_bytes:
            return [], "patch exceeds maximum allowed size."
        if "\0" in patch or "GIT binary patch" in patch:
            return [], "binary patches are not allowed."

        lines = patch.splitlines()
        if not any(line.startswith("--- ") for line in lines):
            return [], "patch must include unified diff '---' headers."
        if not any(line.startswith("+++ ") for line in lines):
            return [], "patch must include unified diff '+++' headers."
        if not any(line.startswith("@@ ") for line in lines):
            return [], "patch must include at least one unified diff hunk."

        files: list[str] = []
        for line in lines:
            if not (line.startswith("--- ") or line.startswith("+++ ")):
                continue
            diff_path = _normalize_diff_header_path(line[4:])
            if diff_path == "/dev/null":
                continue

            resolved = self._resolve_under_root(diff_path)
            if isinstance(resolved, str):
                return [], resolved.removeprefix("ERROR: ")

            relative = resolved.relative_to(self.root).as_posix()
            if relative not in files:
                files.append(relative)
            if len(files) > self.max_patch_files:
                return [], "patch touches too many files."

        if not files:
            return [], "patch does not reference any file under root."
        return files, None

    def _validate_patch_ownership(self, files: list[str]) -> str | None:
        if not self.owned_paths:
            return "apply_patch requires at least one owned path."
        for file_path in files:
            if not any(_owned_paths_overlap(owned_path, file_path) for owned_path in self.owned_paths):
                return f"patch file outside owned paths: {file_path}"
        return None

    def _git_apply_check(self, patch_path: Path) -> dict[str, str]:
        try:
            repo_check = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return {"status": "skipped", "reason": "git executable not found"}
        if repo_check.returncode != 0:
            return {"status": "skipped", "reason": "root is not a git repository"}

        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), "apply", "--check", str(patch_path)],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return {"status": "skipped", "reason": "git executable not found"}
        if completed.returncode == 0:
            return {"status": "passed"}

        detail = (completed.stderr or completed.stdout).strip()
        return {"status": "failed", "detail": _truncate(detail, 1000)}

    def _rg_search_with_binary(
        self,
        pattern: str,
        search_path: Path,
        glob_pattern: str | None,
        max_results: int,
    ) -> str | None:
        command = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
        ]
        if glob_pattern:
            command.extend(["--glob", glob_pattern])
        command.extend(["--", pattern, str(search_path)])

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return None

        if completed.returncode == 1:
            return "NO_MATCHES"
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            return f"ERROR: rg failed: {_truncate(stderr)}"

        lines = completed.stdout.splitlines()[:max_results]
        return "\n".join(lines) if lines else "NO_MATCHES"

    def _rg_search_with_python(
        self,
        pattern: str,
        search_path: Path,
        glob_pattern: str | None,
        max_results: int,
    ) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"

        matches: list[str] = []
        for file_path in _iter_files(search_path):
            if glob_pattern and not _matches_glob(file_path, self.root, glob_pattern):
                continue
            if file_path.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(f"{file_path}:{line_number}:{line}")
                    if len(matches) >= max_results:
                        return "\n".join(matches)

        return "\n".join(matches) if matches else "NO_MATCHES"


class MmxCliClient:
    def __init__(self, config: WorkerConfig, run_dir: Path):
        self.config = config
        self.run_dir = run_dir
        self.step = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tool_paths: list[Path],
        system_prompt: str,
    ) -> dict[str, Any]:
        self.step += 1
        messages_path = self.run_dir / f"messages.step-{self.step:02d}.json"
        _write_json(messages_path, messages)

        command = [
            _resolve_executable(self.config.mmx_bin),
            "text",
            "chat",
            "--messages-file",
            str(messages_path),
            "--system",
            system_prompt,
            "--max-tokens",
            str(self.config.max_tokens),
            "--temperature",
            str(self.config.temperature),
            "--output",
            "json",
            "--non-interactive",
            "--no-color",
            "--timeout",
            str(self.config.timeout),
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        for tool_path in tool_paths:
            command.extend(["--tool", str(tool_path)])

        for attempt in range(self.config.max_retries + 1):
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode == 0:
                return _parse_json_output(completed.stdout)

            detail = (completed.stderr or completed.stdout).strip()
            if (
                attempt >= self.config.max_retries
                or not _is_retryable_mmx_error(detail)
            ):
                raise RuntimeError(f"mmx failed: {_truncate(detail, 2000)}")

            delay = self.config.retry_base_seconds * (2**attempt)
            if delay > 0:
                time.sleep(delay)

        raise RuntimeError("mmx failed after retries")


class MmxWorker:
    def __init__(self, config: WorkerConfig, client: CompletionClient | None = None):
        self.config = WorkerConfig(
            root=Path(config.root).expanduser().resolve(),
            out_dir=Path(config.out_dir).expanduser().resolve(),
            max_steps=config.max_steps,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
            temperature=config.temperature,
            model=config.model,
            mmx_bin=config.mmx_bin,
            max_file_bytes=config.max_file_bytes,
            max_retries=config.max_retries,
            retry_base_seconds=config.retry_base_seconds,
            allow_write=config.allow_write,
            owned_paths=list(config.owned_paths or []),
        )
        self.client = client
        self.tools = ReadOnlyTools(
            root=self.config.root,
            max_file_bytes=self.config.max_file_bytes,
            allow_write=self.config.allow_write,
            owned_paths=self.config.owned_paths,
        )

    def run(self, task: str) -> RunResult:
        if not task.strip():
            raise ValueError("task must be non-empty")

        run_dir = _create_run_dir(self.config.out_dir)
        tool_paths = _write_tool_files(run_dir, allow_write=self.config.allow_write)
        run_log_path = run_dir / "run.jsonl"
        result_path = run_dir / "result.md"
        messages_path = run_dir / "messages.final.json"
        self.tools = ReadOnlyTools(
            root=self.config.root,
            max_file_bytes=self.config.max_file_bytes,
            patch_dir=run_dir / "proposed_patches",
            allow_write=self.config.allow_write,
            owned_paths=self.config.owned_paths,
        )
        client = self.client or MmxCliClient(self.config, run_dir)
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        system_prompt = _system_prompt(
            self.config.root,
            allow_write=self.config.allow_write,
            owned_paths=self.config.owned_paths or [],
        )

        _log_event(run_log_path, {"event": "start", "task": task, "root": str(self.config.root)})

        for step in range(1, self.config.max_steps + 1):
            response = client.complete(messages, tool_paths, system_prompt)
            _log_event(
                run_log_path,
                {
                    "event": "mmx_response",
                    "step": step,
                    "stop_reason": response.get("stop_reason"),
                    "tool_uses": [
                        {"id": item.get("id"), "name": item.get("name")}
                        for item in extract_tool_uses(response)
                    ],
                },
            )

            tool_uses = extract_tool_uses(response)
            if not tool_uses:
                text = extract_text(response) or json.dumps(
                    response, ensure_ascii=False, indent=2
                )
                return self._finish(
                    text=text,
                    run_dir=run_dir,
                    result_path=result_path,
                    messages_path=messages_path,
                    run_log_path=run_log_path,
                    messages=messages,
                )

            messages.append({"role": "assistant", "content": tool_uses})

            tool_results = []
            for tool_use in tool_uses:
                name = str(tool_use.get("name") or "")
                tool_input = tool_use.get("input") or {}
                if not isinstance(tool_input, dict):
                    tool_input = {}

                output = self.tools.execute(name, tool_input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.get("id"),
                        "content": output,
                    }
                )
                _log_event(
                    run_log_path,
                    {
                        "event": "tool_result",
                        "step": step,
                        "tool": name,
                        "tool_use_id": tool_use.get("id"),
                        "result_preview": _truncate(output, 500),
                    },
                )

                if name == "final_answer":
                    messages.append({"role": "user", "content": tool_results})
                    return self._finish(
                        text=output,
                        run_dir=run_dir,
                        result_path=result_path,
                        messages_path=messages_path,
                        run_log_path=run_log_path,
                        messages=messages,
                    )

            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(f"max steps exceeded: {self.config.max_steps}")

    def _finish(
        self,
        text: str,
        run_dir: Path,
        result_path: Path,
        messages_path: Path,
        run_log_path: Path,
        messages: list[dict[str, Any]],
    ) -> RunResult:
        result_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        _write_json(messages_path, messages)
        _log_event(run_log_path, {"event": "finish", "result_path": str(result_path)})
        return RunResult(
            text=text.strip(),
            run_dir=run_dir,
            result_path=result_path,
            messages_path=messages_path,
            run_log_path=run_log_path,
        )


def extract_tool_uses(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in _content_items(response)
        if isinstance(item, dict) and item.get("type") == "tool_use"
    ]


def extract_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content.strip()

    parts = [
        str(item.get("text", ""))
        for item in _content_items(response)
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if parts:
        return "\n".join(part for part in parts if part).strip()

    text = response.get("text")
    return str(text).strip() if text is not None else ""


def review_patch(root: Path, patch_path: Path, apply: bool = False) -> PatchReviewResult:
    resolved_root = Path(root).expanduser().resolve()
    resolved_patch = Path(patch_path).expanduser().resolve()
    if not resolved_patch.exists():
        raise FileNotFoundError(f"patch not found: {resolved_patch}")
    if not resolved_patch.is_file():
        raise ValueError(f"patch path is not a file: {resolved_patch}")

    patch = resolved_patch.read_text(encoding="utf-8", errors="replace")
    tools = ReadOnlyTools(root=resolved_root)
    files, error = tools._validate_unified_patch(patch)
    if error:
        raise ValueError(error)

    check = tools._git_apply_check(resolved_patch)
    if not apply:
        return PatchReviewResult(
            status="ready",
            root=resolved_root,
            patch_path=resolved_patch,
            files=files,
            git_apply_check=check,
            applied=False,
        )

    if check.get("status") != "passed":
        reason = check.get("detail") or check.get("reason") or "git apply check did not pass"
        raise RuntimeError(f"cannot apply patch: {reason}")

    completed = subprocess.run(
        ["git", "-C", str(resolved_root), "apply", str(resolved_patch)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git apply failed: {_truncate(detail, 2000)}")

    return PatchReviewResult(
        status="applied",
        root=resolved_root,
        patch_path=resolved_patch,
        files=files,
        git_apply_check=check,
        applied=True,
    )


def install_stable_tool(source_dir: Path, target_dir: Path) -> InstallResult:
    resolved_source = Path(source_dir).expanduser().resolve()
    resolved_target = Path(target_dir).expanduser().resolve()
    if not resolved_source.exists():
        raise FileNotFoundError(f"source directory not found: {resolved_source}")
    if not (resolved_source / "mmx_worker.py").is_file():
        raise FileNotFoundError(f"mmx_worker.py not found in: {resolved_source}")

    copied: list[str] = []
    resolved_target.mkdir(parents=True, exist_ok=True)
    for item in resolved_source.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(resolved_source)
        if _is_install_ignored(relative):
            continue
        destination = resolved_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied.append(relative.as_posix())

    copied.sort()
    manifest_path = resolved_target / "INSTALL_MANIFEST.json"
    manifest = {
        "status": "installed",
        "source_dir": str(resolved_source),
        "target_dir": str(resolved_target),
        "files": copied,
        "installed_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(manifest_path, manifest)
    return InstallResult(
        status="installed",
        source_dir=resolved_source,
        target_dir=resolved_target,
        manifest_path=manifest_path,
        files=copied,
    )


def load_batch_tasks(tasks_file: Path, default_root: Path | None = None) -> list[BatchTask]:
    resolved = Path(tasks_file).expanduser().resolve()
    values = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("tasks file must contain a JSON array")

    tasks: list[BatchTask] = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"task #{index} must be an object")
        task_text = str(value.get("task") or "").strip()
        if not task_text:
            raise ValueError(f"task #{index} requires non-empty 'task'")
        task_id = str(value.get("id") or f"task-{index:03d}").strip()
        root_value = value.get("root") or default_root
        if root_value is None:
            raise ValueError(f"task #{index} requires 'root' or --default-root")
        tasks.append(
            BatchTask(
                id=_safe_task_id(task_id),
                task=task_text,
                root=Path(root_value),
                max_steps=_positive_int(value.get("max_steps"), 12),
                owns=_normalize_owns(value.get("owns")),
                allow_write=bool(value.get("allow_write", False)),
            )
        )
    return tasks


def validate_batch_ownership(tasks: list[BatchTask]) -> dict[str, str]:
    owner_map: dict[str, str] = {}
    for task in tasks:
        for owned_path in task.owns or []:
            normalized = _normalize_owned_path(owned_path)
            for existing_path, existing_task_id in owner_map.items():
                if _owned_paths_overlap(existing_path, normalized):
                    raise ValueError(
                        "ownership conflict: "
                        f"{normalized} overlaps {existing_path} claimed by {existing_task_id} "
                        f"and {task.id}"
                    )
            owner_map[normalized] = task.id
    return owner_map


def run_batch_tasks(
    tasks: list[BatchTask],
    out_dir: Path,
    parallel: int = 1,
    client_factory: Callable[[BatchTask], CompletionClient | None] | None = None,
    worktree_base: Path | None = None,
) -> BatchRunResult:
    resolved_out = Path(out_dir).expanduser().resolve()
    resolved_out.mkdir(parents=True, exist_ok=True)
    if not tasks:
        raise ValueError("at least one batch task is required")
    owner_map = validate_batch_ownership(tasks)

    if worktree_base is not None:
        tasks = [
            _task_with_isolated_worktree(task, Path(worktree_base))
            for task in tasks
        ]

    worker_count = max(1, int(parallel))
    if worker_count == 1:
        results = [
            _run_one_batch_task(task, resolved_out, client_factory)
            for task in tasks
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(_run_one_batch_task, task, resolved_out, client_factory)
                for task in tasks
            ]
            results = [future.result() for future in futures]

    status = "completed" if all(item.status == "completed" for item in results) else "failed"
    summary_path = resolved_out / "batch.summary.json"
    ownership_path = resolved_out / "ownership.json"
    _write_json(ownership_path, owner_map)
    _write_json(
        summary_path,
        {
            "status": status,
            "out_dir": str(resolved_out),
            "ownership_path": str(ownership_path),
            "results": [
                {
                    "task_id": item.task_id,
                    "status": item.status,
                    "text": item.text,
                    "run_dir": str(item.run_dir) if item.run_dir else None,
                    "error": item.error,
                }
                for item in results
            ],
        },
    )
    return BatchRunResult(
        status=status,
        out_dir=resolved_out,
        summary_path=summary_path,
        results=results,
    )


def create_isolated_worktree(
    repo_root: Path,
    worktree_base: Path,
    task_id: str,
) -> WorktreeResult:
    resolved_repo = Path(repo_root).expanduser().resolve()
    resolved_base = Path(worktree_base).expanduser().resolve()
    if not resolved_repo.exists():
        raise FileNotFoundError(f"repo root not found: {resolved_repo}")

    repo_check = subprocess.run(
        ["git", "-C", str(resolved_repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if repo_check.returncode != 0:
        detail = (repo_check.stderr or repo_check.stdout).strip()
        raise RuntimeError(f"not a git worktree: {_truncate(detail, 1000)}")

    safe_id = _safe_task_id(task_id)
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    branch = f"mmx-worker-{safe_id}-{stamp}"
    path = resolved_base / branch
    counter = 1
    while path.exists():
        counter += 1
        branch = f"mmx-worker-{safe_id}-{stamp}-{counter}"
        path = resolved_base / branch

    resolved_base.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "-C", str(resolved_repo), "worktree", "add", "-b", branch, str(path), "HEAD"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git worktree add failed: {_truncate(detail, 2000)}")

    return WorktreeResult(
        status="created",
        repo_root=resolved_repo,
        path=path,
        branch=branch,
    )


def tool_schemas(allow_write: bool = False) -> list[dict[str, Any]]:
    schemas = [
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file under the allowed root and return numbered lines.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or root-relative file path."},
                    "start_line": {"type": "integer", "description": "1-based start line."},
                    "max_lines": {"type": "integer", "description": "Maximum lines to return."},
                },
                "required": ["path"],
            },
        },
        {
            "name": "rg_search",
            "description": "Search text under the allowed root. Returns path:line:match lines.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "path": {"type": "string", "description": "Absolute or root-relative search path."},
                    "glob": {"type": "string", "description": "Optional file glob, such as *.md."},
                    "max_results": {"type": "integer", "description": "Maximum matches to return."},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "final_answer",
            "description": "Return the final answer to the lead agent and stop the worker loop.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Final concise answer."},
                    "status": {"type": "string", "description": "done, blocked, or needs_review."},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Evidence paths or line references.",
                    },
                },
                "required": ["summary"],
            },
        },
        {
            "name": "list_dir",
            "description": "List directory contents under the allowed root with file sizes.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or root-relative directory path. Defaults to root."},
                    "recursive": {"type": "boolean", "description": "List recursively. Default false."},
                    "max_depth": {"type": "integer", "description": "Max recursion depth (1-5). Default 3."},
                    "max_entries": {"type": "integer", "description": "Max entries to return. Default 200."},
                },
                "required": [],
            },
        },
        {
            "name": "glob_find",
            "description": "Find files matching a glob pattern under the allowed root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."},
                    "path": {"type": "string", "description": "Absolute or root-relative search path. Defaults to root."},
                    "max_results": {"type": "integer", "description": "Maximum matches to return. Default 50."},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "run_command",
            "description": (
                "Run a constrained read-only command under the allowed root. Shell syntax, "
                "root-external paths, and write-oriented commands are rejected. Write mode "
                "does not expand run_command permissions; use apply_patch for file changes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (1-60). Default 15."},
                },
                "required": ["command"],
            },
        },
        {
            "name": "propose_patch",
            "description": (
                "Submit a unified diff for lead-agent review. This only saves the "
                "patch artifact; it does not apply changes to disk."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Unified diff text with ---/+++/@@ headers.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short explanation of the proposed change.",
                    },
                },
                "required": ["patch"],
            },
        },
    ]
    if allow_write:
        schemas.append(
            {
                "name": "apply_patch",
                "description": (
                    "Apply a unified diff inside the allowed root. This only works when "
                    "write mode is enabled and every touched file is inside owned paths."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patch": {
                            "type": "string",
                            "description": "Unified diff text with ---/+++/@@ headers.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Short explanation of the implemented change.",
                        },
                    },
                    "required": ["patch"],
                },
            }
        )
    return schemas


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "review-patch":
        return _main_review_patch(argv[1:])
    if argv and argv[0] == "install-stable":
        return _main_install_stable(argv[1:])
    if argv and argv[0] == "run-batch":
        return _main_run_batch(argv[1:])
    if argv and argv[0] == "create-worktree":
        return _main_create_worktree(argv[1:])

    args = _parse_args(argv)
    config = WorkerConfig(
        root=Path(args.root),
        out_dir=Path(args.out_dir),
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        temperature=args.temperature,
        model=args.model,
        mmx_bin=args.mmx_bin,
        max_retries=args.max_retries,
        retry_base_seconds=args.retry_base_seconds,
        allow_write=args.allow_write,
        owned_paths=_normalize_owns(args.owns),
    )
    worker = MmxWorker(config=config)
    result = worker.run(args.task)
    print(result.text)
    print(f"\nArtifacts: {result.run_dir}", file=sys.stderr)
    return 0


def _main_review_patch(argv: list[str]) -> int:
    args = _parse_review_patch_args(argv)
    try:
        result = review_patch(
            root=Path(args.root),
            patch_path=Path(args.patch),
            apply=args.apply,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(_format_patch_review(result))
    return 0


def _main_install_stable(argv: list[str]) -> int:
    args = _parse_install_stable_args(argv)
    try:
        result = install_stable_tool(
            source_dir=Path(args.source),
            target_dir=Path(args.target),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Source: {result.source_dir}")
    print(f"Target: {result.target_dir}")
    print(f"Manifest: {result.manifest_path}")
    print(f"Files: {len(result.files)}")
    return 0


def _main_run_batch(argv: list[str]) -> int:
    args = _parse_run_batch_args(argv)
    try:
        tasks = load_batch_tasks(
            tasks_file=Path(args.tasks_file),
            default_root=Path(args.default_root) if args.default_root else None,
        )
        owner_map = validate_batch_ownership(tasks)
        if args.dry_run:
            print("Status: dry-run")
            print(f"Tasks: {len(tasks)}")
            print(f"Owned paths: {len(owner_map)}")
            for task in tasks:
                print(f"- {task.id}: root={Path(task.root).expanduser().resolve()}")
            return 0
        result = run_batch_tasks(
            tasks=tasks,
            out_dir=Path(args.out_dir),
            parallel=args.parallel,
            worktree_base=Path(args.worktree_base) if args.worktree_base else None,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Summary: {result.summary_path}")
    for item in result.results:
        suffix = f" ({item.error})" if item.error else ""
        print(f"- {item.task_id}: {item.status}{suffix}")
    return 0 if result.status == "completed" else 1


def _main_create_worktree(argv: list[str]) -> int:
    args = _parse_create_worktree_args(argv)
    try:
        result = create_isolated_worktree(
            repo_root=Path(args.root),
            worktree_base=Path(args.worktree_base),
            task_id=args.task_id,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Repo: {result.repo_root}")
    print(f"Worktree: {result.path}")
    print(f"Branch: {result.branch}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a read-only MMX worker loop.")
    parser.add_argument("--task", required=True, help="Worker task.")
    parser.add_argument("--root", default=".", help="Allowed filesystem root.")
    parser.add_argument(
        "--out-dir",
        default="mmx-subagent-runs",
        help="Directory for run artifacts.",
    )
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--model", default=None)
    parser.add_argument("--mmx-bin", default="mmx")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-base-seconds", type=float, default=1.0)
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Expose apply_patch so the worker can write inside the allowed root.",
    )
    parser.add_argument(
        "--owns",
        action="append",
        default=[],
        help="Root-relative file or directory owned by this worker. Repeatable.",
    )
    return parser.parse_args(argv)


def _parse_run_batch_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run multiple MMX worker tasks.")
    parser.add_argument("--tasks-file", required=True, help="JSON array of batch tasks.")
    parser.add_argument(
        "--out-dir",
        default="mmx-subagent-batch-runs",
        help="Batch output directory.",
    )
    parser.add_argument("--default-root", default=None)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Validate task loading without calling mmx.")
    parser.add_argument(
        "--worktree-base",
        default=None,
        help="Optional base directory for per-task git worktrees.",
    )
    return parser.parse_args(argv)


def _parse_create_worktree_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create an isolated git worktree for one task.")
    parser.add_argument("--root", required=True, help="Source git repository root.")
    parser.add_argument("--worktree-base", required=True, help="Directory for worker worktrees.")
    parser.add_argument("--task-id", required=True, help="Task id used in branch/worktree names.")
    return parser.parse_args(argv)


def _parse_install_stable_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install MMX worker as a stable local tool.")
    parser.add_argument(
        "--source",
        default=str(Path(__file__).resolve().parent),
        help="Source directory containing mmx_worker.py.",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_STABLE_TOOL_DIR),
        help="Stable installation target directory.",
    )
    return parser.parse_args(argv)


def _parse_review_patch_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review a proposed MMX worker patch artifact."
    )
    parser.add_argument("--root", required=True, help="Target project root.")
    parser.add_argument("--patch", required=True, help="Path to patch-NNN.diff.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the patch after validation. Omit for dry-run review.",
    )
    return parser.parse_args(argv)


def _format_patch_review(result: PatchReviewResult) -> str:
    lines = [
        f"Status: {result.status}",
        f"Root: {result.root}",
        f"Patch: {result.patch_path}",
        f"Applied: {'yes' if result.applied else 'no'}",
        f"git apply --check: {result.git_apply_check.get('status', 'unknown')}",
    ]
    if result.git_apply_check.get("reason"):
        lines.append(f"Check reason: {result.git_apply_check['reason']}")
    if result.git_apply_check.get("detail"):
        lines.append(f"Check detail: {result.git_apply_check['detail']}")
    lines.append("Files:")
    lines.extend(f"- {file_path}" for file_path in result.files)
    return "\n".join(lines)


def _run_one_batch_task(
    task: BatchTask,
    out_dir: Path,
    client_factory: Callable[[BatchTask], CompletionClient | None] | None,
) -> BatchItemResult:
    task_out_dir = out_dir / task.id
    try:
        client = client_factory(task) if client_factory else None
        worker = MmxWorker(
            config=WorkerConfig(
                root=task.root,
                out_dir=task_out_dir,
                max_steps=task.max_steps,
                allow_write=task.allow_write,
                owned_paths=task.owns or [],
            ),
            client=client,
        )
        result = worker.run(task.task)
        return BatchItemResult(
            task_id=task.id,
            status="completed",
            text=result.text,
            run_dir=result.run_dir,
        )
    except Exception as exc:
        return BatchItemResult(
            task_id=task.id,
            status="failed",
            text="",
            run_dir=None,
            error=str(exc),
        )


def _task_with_isolated_worktree(task: BatchTask, worktree_base: Path) -> BatchTask:
    result = create_isolated_worktree(
        repo_root=task.root,
        worktree_base=worktree_base,
        task_id=task.id,
    )
    return BatchTask(
        id=task.id,
        task=task.task,
        root=result.path,
        max_steps=task.max_steps,
        owns=task.owns,
        allow_write=task.allow_write,
    )


def _content_items(response: dict[str, Any]) -> list[Any]:
    content = response.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    message = response.get("message")
    if isinstance(message, dict):
        message_content = message.get("content")
        if isinstance(message_content, list):
            return message_content
        if isinstance(message_content, str):
            return [{"type": "text", "text": message_content}]

    return []


def _write_tool_files(run_dir: Path, allow_write: bool = False) -> list[Path]:
    tools_dir = run_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    schemas = tool_schemas(allow_write=allow_write)
    _write_json(run_dir / "tools.json", schemas)

    paths: list[Path] = []
    for schema in schemas:
        path = tools_dir / f"{schema['name']}.json"
        _write_json(path, schema)
        paths.append(path)
    return paths


def _system_prompt(root: Path, allow_write: bool = False, owned_paths: list[str] | None = None) -> str:
    owned = ", ".join(owned_paths or [])
    if allow_write:
        write_instruction = (
            "You may call apply_patch to implement changes, but only for files under owned paths "
            f"[{owned}]. Use unified diffs. Do not use run_command for writes. "
        )
    else:
        write_instruction = (
            "Use propose_patch for file modifications; it only proposes unified diffs for review. "
            "Do not modify files directly. Do not use run_command for writes. "
        )
    return (
        "You are a controlled worker used by a lead coding agent. "
        f"Allowed filesystem root: {root}. "
        "Use read_file, rg_search, list_dir, and glob_find for evidence. "
        "Use run_command only for constrained read-only commands; shell syntax and root-external paths are rejected. "
        f"{write_instruction}"
        "When done, call final_answer with summary, status, and evidence."
    )


def _create_run_dir(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = out_dir / stamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = out_dir / f"{stamp}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _log_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _parse_json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise RuntimeError("mmx returned empty stdout")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise RuntimeError("mmx JSON output must be an object")
    return value


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    return resolved or name


def _is_install_ignored(relative: Path) -> bool:
    parts = set(relative.parts)
    ignored_dirs = {
        "__pycache__",
        ".pytest_cache",
        ".git",
    }
    if parts & ignored_dirs:
        return True
    return relative.suffix in {".pyc", ".pyo"}


def _safe_task_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "task"


def _validate_glob_pattern(pattern: str) -> str | None:
    normalized = pattern.replace("\\", "/")
    if _is_absolute_path_text(normalized):
        return "glob pattern outside root: absolute patterns are not allowed."
    if _contains_parent_traversal(normalized):
        return "glob pattern outside root: parent traversal is not allowed."
    return None


def _parse_read_only_command(command: str, root: Path) -> tuple[list[str], str | None]:
    if SHELL_METACHAR_PATTERN.search(command):
        return [], "shell metacharacter is not allowed in run_command."
    try:
        parts = shlex.split(command, posix=False)
    except ValueError as exc:
        return [], f"could not parse command: {exc}"

    args = [_strip_arg_quotes(part) for part in parts if part.strip()]
    if not args:
        return [], "run_command requires a non-empty command."
    if _command_executable_is_path(args[0]):
        return [], "command executable must be an allowlisted name, not a path."

    allowlist_error = _validate_read_only_command_allowlist(args)
    if allowlist_error:
        return [], allowlist_error

    path_error = _validate_command_path_scope(args, root)
    if path_error:
        return [], path_error
    return args, None


def _validate_read_only_command_allowlist(args: list[str]) -> str | None:
    command = _command_name(args[0])
    if command == "git":
        return _validate_git_command(args)
    if command in {"python", "python3", "node", "rg"}:
        if args[1:] == ["--version"]:
            return None
        return f"{command} is only allowed with --version."
    if command in {"pip", "pip3"}:
        if len(args) >= 2 and args[1] in {"list", "show"}:
            return None
        return "pip is only allowed with list or show."
    if command == "npm":
        if len(args) >= 2 and args[1] in {"ls", "list"}:
            return None
        return "npm is only allowed with ls or list."
    if command in {"cat", "type", "ls", "find", "wc", "head", "tail", "file", "echo"}:
        return None
    if command in {"which", "where"}:
        if len(args) < 2:
            return f"{command} requires at least one executable name."
        for value in args[1:]:
            if _command_executable_is_path(value):
                return f"{command} arguments must be executable names, not paths."
        return None
    return "command not in safe read-only allowlist."


def _validate_git_command(args: list[str]) -> str | None:
    if len(args) < 2:
        return "git command requires an allowlisted subcommand."
    for arg in args[1:]:
        if (
            arg == "-C"
            or arg.startswith("-C")
            or arg == "-c"
            or arg.startswith("--git-dir")
            or arg.startswith("--work-tree")
        ):
            return "git directory override options are not allowed."

    subcommand = args[1]
    if subcommand == "--version":
        return None if len(args) == 2 else "git --version does not accept extra arguments."
    if subcommand == "remote":
        return None if args[2:] == ["-v"] else "git remote is only allowed as git remote -v."
    if subcommand in {"log", "diff", "show", "status", "branch", "ls-files", "ls-tree"}:
        return None
    return f"git subcommand is not allowed: {subcommand}"


def _validate_command_path_scope(args: list[str], root: Path) -> str | None:
    command = _command_name(args[0])
    file_arg_commands = {"cat", "type", "ls", "find", "wc", "head", "tail", "file"}
    for index, raw_arg in enumerate(args[1:], start=1):
        values = _path_candidate_values(raw_arg)
        for value in values:
            if _contains_parent_traversal(value):
                return "command path outside root: parent traversal is not allowed."
            if _is_absolute_path_text(value):
                error = _validate_path_under_root(value, root)
                if error:
                    return error
        if command in file_arg_commands and _is_file_command_path_arg(args, index):
            error = _validate_path_under_root(raw_arg, root)
            if error:
                return error
    return None


def _path_candidate_values(value: str) -> list[str]:
    cleaned = _strip_arg_quotes(value)
    values = [cleaned]
    if "=" in cleaned:
        values.append(cleaned.split("=", 1)[1])
    return [item for item in values if item]


def _is_file_command_path_arg(args: list[str], index: int) -> bool:
    value = args[index]
    if value == "--":
        return False
    if value.startswith("-"):
        return False
    previous = args[index - 1] if index > 1 else ""
    if previous in {"-n", "--lines", "-c", "--bytes", "-name", "-type", "-maxdepth"}:
        return False
    return True


def _validate_path_under_root(value: str, root: Path) -> str | None:
    cleaned = _strip_arg_quotes(value)
    if cleaned in {"", "."}:
        return None
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return f"command path outside root: {resolved}"
    return None


def _command_executable_is_path(value: str) -> bool:
    cleaned = _strip_arg_quotes(value).replace("\\", "/")
    return "/" in cleaned or _is_absolute_path_text(cleaned) or _contains_parent_traversal(cleaned)


def _command_name(value: str) -> str:
    cleaned = _strip_arg_quotes(value).lower()
    return cleaned.removesuffix(".exe")


def _strip_arg_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_absolute_path_text(value: str) -> bool:
    return value.startswith("/") or value.startswith("//") or re.match(r"^[A-Za-z]:/", value) is not None


def _contains_parent_traversal(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _normalize_owns(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'owns' must be an array of paths")
    normalized: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(_normalize_owned_path(text))
    return normalized


def _normalize_owned_path(value: str) -> str:
    path = value.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        raise ValueError(f"owned path must be root-relative: {value}")
    parts = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError(f"owned path cannot contain '..': {value}")
        parts.append(part)
    if not parts:
        raise ValueError("owned path cannot be empty")
    return "/".join(parts)


def _owned_paths_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


def _normalize_diff_header_path(value: str) -> str:
    path = value.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    elif " " in path:
        path = path.split(" ", 1)[0]
    path = path.strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _is_retryable_mmx_error(detail: str) -> bool:
    lower = detail.lower()
    retryable_markers = [
        "network request failed",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "econnreset",
        "etimedout",
        "socket hang up",
    ]
    return any(marker in lower for marker in retryable_markers)


def _iter_files(path: Path):
    if path.is_file():
        yield path
        return

    for file_path in path.rglob("*"):
        if file_path.is_file():
            yield file_path


def _matches_glob(file_path: Path, root: Path, pattern: str) -> bool:
    try:
        relative = file_path.relative_to(root).as_posix()
    except ValueError:
        relative = file_path.name
    return fnmatch.fnmatch(file_path.name, pattern) or fnmatch.fnmatch(relative, pattern)


def _truncate(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


if __name__ == "__main__":
    raise SystemExit(main())
