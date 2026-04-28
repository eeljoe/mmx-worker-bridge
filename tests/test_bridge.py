import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmx_worker_bridge import (
    BatchTask,
    MmxCliClient,
    MmxWorker,
    ReadOnlyTools,
    WorkerConfig,
    _resolve_executable,
    create_isolated_worktree,
    extract_text,
    extract_tool_uses,
    install_stable_tool,
    load_batch_tasks,
    main,
    review_patch,
    run_batch_tasks,
    validate_batch_ownership,
)


def test_read_file_returns_numbered_lines_and_rejects_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    sample = root / "sample.txt"
    sample.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    tools = ReadOnlyTools(root=root)

    assert tools.read_file({"path": str(sample), "start_line": 2, "max_lines": 2}) == (
        f"{sample}:2: beta\n"
        f"{sample}:3: gamma"
    )

    result = tools.read_file({"path": str(outside)})

    assert "ERROR" in result
    assert "outside root" in result


def test_rg_search_finds_matches_with_line_numbers(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.md"
    second = root / "second.txt"
    first.write_text("agent loop\nno match\nMiniMax tool_use\n", encoding="utf-8")
    second.write_text("tool_result closes loop\n", encoding="utf-8")

    tools = ReadOnlyTools(root=root)

    result = tools.rg_search({"pattern": "tool", "path": str(root), "max_results": 3})

    assert f"{first}:3:MiniMax tool_use" in result
    assert f"{second}:1:tool_result closes loop" in result


def test_run_command_rejects_shell_chaining_in_read_only_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tools = ReadOnlyTools(root=root)

    result = tools.run_command({"command": "echo allowed & echo injected", "timeout": 5})

    assert "ERROR" in result
    assert "shell metacharacter" in result
    assert "injected" not in result


def test_run_command_rejects_root_external_file_arguments(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret-sentinel\n", encoding="utf-8")
    tools = ReadOnlyTools(root=root)

    result = tools.run_command({"command": f'type "{outside}"', "timeout": 5})

    assert "ERROR" in result
    assert "outside root" in result
    assert "outside-secret-sentinel" not in result


def test_run_command_allows_git_status_under_root(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for run_command git status")

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    tools = ReadOnlyTools(root=root)

    result = tools.run_command({"command": "git status --short", "timeout": 5})

    assert "ERROR" not in result


def test_glob_find_rejects_parent_traversal_pattern(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret-sentinel\n", encoding="utf-8")
    tools = ReadOnlyTools(root=root)

    result = tools.glob_find({"pattern": "../outside.txt"})

    assert "ERROR" in result
    assert "outside root" in result
    assert "outside.txt" not in result


def test_propose_patch_saves_unified_diff_and_rejects_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    patch_dir = tmp_path / "patches"
    tools = ReadOnlyTools(root=root, patch_dir=patch_dir)
    patch = """--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-old
+new
"""

    result = json.loads(
        tools.propose_patch({"patch": patch, "summary": "change example text"})
    )

    assert result["status"] == "saved"
    assert result["patch_id"] == "patch-001"
    assert result["files"] == ["example.txt"]
    assert (patch_dir / "patch-001.diff").read_text(encoding="utf-8") == patch
    assert (patch_dir / "patch-001.json").exists()

    outside_patch = """--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-secret
+changed
"""
    rejected = tools.propose_patch({"patch": outside_patch})

    assert "ERROR" in rejected
    assert "outside root" in rejected


def test_apply_patch_requires_write_enabled(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    patch_dir = tmp_path / "patches"
    tools = ReadOnlyTools(root=root, patch_dir=patch_dir)
    patch = """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
"""

    result = tools.apply_patch({"patch": patch, "summary": "change example"})

    assert "ERROR" in result
    assert "not enabled" in result


def test_apply_patch_applies_owned_patch_in_git_root(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for apply_patch")

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    sample = root / "example.txt"
    sample.write_text("old\n", encoding="utf-8")
    patch_dir = tmp_path / "patches"
    tools = ReadOnlyTools(
        root=root,
        patch_dir=patch_dir,
        allow_write=True,
        owned_paths=["example.txt"],
    )
    patch = """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
"""

    result = json.loads(tools.apply_patch({"patch": patch, "summary": "change example"}))

    assert result["status"] == "applied"
    assert result["patch_id"] == "patch-001"
    assert result["files"] == ["example.txt"]
    assert result["git_apply_check"]["status"] == "passed"
    assert sample.read_text(encoding="utf-8") == "new\n"


def test_apply_patch_rejects_unowned_patch(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    patch_dir = tmp_path / "patches"
    tools = ReadOnlyTools(
        root=root,
        patch_dir=patch_dir,
        allow_write=True,
        owned_paths=["allowed.txt"],
    )
    patch = """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
"""

    result = tools.apply_patch({"patch": patch, "summary": "change example"})

    assert "ERROR" in result
    assert "outside owned paths" in result


def test_extracts_tool_uses_and_text_from_mmx_response():
    response = {
        "content": [
            {"type": "text", "text": "I will inspect the file."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "read_file",
                "input": {"path": "report.md"},
            },
        ]
    }

    assert extract_tool_uses(response) == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "report.md"},
        }
    ]
    assert extract_text({"content": [{"type": "text", "text": "done"}]}) == "done"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tool_paths, system_prompt):
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "tool_paths": [str(path) for path in tool_paths],
                "system_prompt": system_prompt,
            }
        )
        return self.responses.pop(0)


def test_worker_executes_tool_loop_and_writes_artifacts(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    sample = root / "report.md"
    sample.write_text("line one\nagent loop evidence\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    fake_client = FakeClient(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_read",
                        "name": "read_file",
                        "input": {"path": str(sample), "max_lines": 2},
                    }
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": [
                    {
                        "type": "text",
                        "text": "The report contains agent loop evidence.",
                    }
                ],
                "stop_reason": "end_turn",
            },
        ]
    )
    config = WorkerConfig(root=root, out_dir=out_dir, max_steps=3)
    worker = MmxWorker(config=config, client=fake_client)

    result = worker.run("Inspect the report.")

    assert result.text == "The report contains agent loop evidence."
    assert result.result_path.exists()
    assert result.messages_path.exists()
    assert result.run_log_path.exists()
    assert len(fake_client.calls) == 2

    final_messages = json.loads(result.messages_path.read_text(encoding="utf-8"))
    assert final_messages[1]["content"][0]["name"] == "read_file"
    assert final_messages[2]["content"][0]["tool_use_id"] == "call_read"


def test_worker_stops_when_final_answer_tool_is_called(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "runs"
    fake_client = FakeClient(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_final",
                        "name": "final_answer",
                        "input": {
                            "summary": "Read-only scout completed.",
                            "status": "done",
                            "evidence": ["report.md:1"],
                        },
                    }
                ],
                "stop_reason": "tool_use",
            }
        ]
    )
    config = WorkerConfig(root=root, out_dir=out_dir, max_steps=3)
    worker = MmxWorker(config=config, client=fake_client)

    result = worker.run("Finish through final_answer.")

    assert "Read-only scout completed." in result.text
    assert "report.md:1" in result.text


def test_worker_executes_propose_patch_then_final_answer(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "runs"
    patch = """--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-old
+new
"""
    fake_client = FakeClient(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_patch",
                        "name": "propose_patch",
                        "input": {"patch": patch, "summary": "change example"},
                    }
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_final",
                        "name": "final_answer",
                        "input": {
                            "summary": "Patch proposal produced.",
                            "status": "needs_review",
                            "evidence": ["patch-001.diff"],
                        },
                    }
                ],
                "stop_reason": "tool_use",
            },
        ]
    )
    worker = MmxWorker(
        config=WorkerConfig(root=root, out_dir=out_dir, max_steps=3),
        client=fake_client,
    )

    result = worker.run("Propose a patch.")

    patch_file = result.run_dir / "proposed_patches" / "patch-001.diff"
    assert patch_file.exists()
    assert "Patch proposal produced." in result.text
    final_messages = json.loads(result.messages_path.read_text(encoding="utf-8"))
    patch_tool_result = final_messages[2]["content"][0]["content"]
    assert "patch-001" in patch_tool_result


def test_worker_executes_apply_patch_when_write_enabled(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for apply_patch")

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    sample = root / "example.txt"
    sample.write_text("old\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    patch = """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
"""
    fake_client = FakeClient(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_apply",
                        "name": "apply_patch",
                        "input": {"patch": patch, "summary": "change example"},
                    }
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_final",
                        "name": "final_answer",
                        "input": {
                            "summary": "Patch applied in worker root.",
                            "status": "done",
                            "evidence": ["patch-001.diff"],
                        },
                    }
                ],
                "stop_reason": "tool_use",
            },
        ]
    )
    worker = MmxWorker(
        config=WorkerConfig(
            root=root,
            out_dir=out_dir,
            max_steps=3,
            allow_write=True,
            owned_paths=["example.txt"],
        ),
        client=fake_client,
    )

    result = worker.run("Apply a patch.")

    assert sample.read_text(encoding="utf-8") == "new\n"
    assert (result.run_dir / "proposed_patches" / "patch-001.diff").exists()
    assert "Patch applied in worker root." in result.text


def test_review_patch_dry_run_reports_files_without_modifying(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    sample = root / "example.txt"
    sample.write_text("old\n", encoding="utf-8")
    patch_path = tmp_path / "patch.diff"
    patch_path.write_text(
        """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
""",
        encoding="utf-8",
    )

    result = review_patch(root=root, patch_path=patch_path, apply=False)

    assert result.status == "ready"
    assert result.files == ["example.txt"]
    assert result.applied is False
    assert sample.read_text(encoding="utf-8") == "old\n"


def test_review_patch_apply_changes_file_in_git_root(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for apply verification")

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    sample = root / "example.txt"
    sample.write_text("old\n", encoding="utf-8")
    patch_path = tmp_path / "patch.diff"
    patch_path.write_text(
        """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
""",
        encoding="utf-8",
    )

    result = review_patch(root=root, patch_path=patch_path, apply=True)

    assert result.status == "applied"
    assert result.applied is True
    assert result.git_apply_check["status"] == "passed"
    assert sample.read_text(encoding="utf-8") == "new\n"


def test_review_patch_rejects_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    patch_path = tmp_path / "outside.diff"
    patch_path.write_text(
        """--- ../outside.txt
+++ ../outside.txt
@@ -1 +1 @@
-old
+new
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside root"):
        review_patch(root=root, patch_path=patch_path, apply=False)


def test_install_stable_tool_copies_runtime_files_and_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "mmx_worker.py").write_text("print('worker')\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_mmx_worker.py").write_text("# tests\n", encoding="utf-8")
    (source / "docs").mkdir()
    (source / "docs" / "README.md").write_text("# docs\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    target = tmp_path / "target"

    result = install_stable_tool(source_dir=source, target_dir=target)

    assert result.status == "installed"
    assert (target / "mmx_worker.py").read_text(encoding="utf-8") == "print('worker')\n"
    assert (target / "tests" / "test_mmx_worker.py").exists()
    assert (target / "docs" / "README.md").exists()
    assert not (target / "__pycache__").exists()
    manifest = json.loads((target / "INSTALL_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["source_dir"] == str(source.resolve())
    assert "mmx_worker.py" in manifest["files"]


def test_run_batch_tasks_executes_fake_workers_and_writes_summary(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "batch-runs"
    tasks = [
        BatchTask(id="task-a", task="Inspect A", root=root),
        BatchTask(id="task-b", task="Inspect B", root=root),
    ]

    def client_factory(task):
        return FakeClient(
            [
                {
                    "content": [
                        {"type": "text", "text": f"completed {task.id}"},
                    ],
                    "stop_reason": "end_turn",
                }
            ]
        )

    result = run_batch_tasks(
        tasks=tasks,
        out_dir=out_dir,
        parallel=1,
        client_factory=client_factory,
    )

    assert result.status == "completed"
    assert [item.task_id for item in result.results] == ["task-a", "task-b"]
    assert [item.status for item in result.results] == ["completed", "completed"]
    assert result.summary_path.exists()
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["results"][0]["text"] == "completed task-a"
    assert summary["results"][1]["text"] == "completed task-b"


def test_run_batch_tasks_parallel_executes_workers_concurrently(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "batch-runs"
    tasks = [
        BatchTask(id="task-a", task="Inspect A", root=root),
        BatchTask(id="task-b", task="Inspect B", root=root),
    ]
    started = []
    release = threading.Event()
    lock = threading.Lock()

    class BlockingClient:
        def __init__(self, task_id):
            self.task_id = task_id

        def complete(self, messages, tool_paths, system_prompt):
            with lock:
                started.append(self.task_id)
                if len(started) == 2:
                    release.set()
            assert release.wait(timeout=2)
            return {
                "content": [{"type": "text", "text": f"completed {self.task_id}"}],
                "stop_reason": "end_turn",
            }

    started_at = time.perf_counter()
    result = run_batch_tasks(
        tasks=tasks,
        out_dir=out_dir,
        parallel=2,
        client_factory=lambda task: BlockingClient(task.id),
    )
    elapsed = time.perf_counter() - started_at

    assert result.status == "completed"
    assert sorted(started) == ["task-a", "task-b"]
    assert elapsed < 2


def test_run_batch_tasks_passes_write_controls_to_worker(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for apply_patch")

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    sample = root / "example.txt"
    sample.write_text("old\n", encoding="utf-8")
    out_dir = tmp_path / "batch-runs"
    patch = """--- example.txt
+++ example.txt
@@ -1 +1 @@
-old
+new
"""
    tasks = [
        BatchTask(
            id="task-a",
            task="Apply owned patch",
            root=root,
            owns=["example.txt"],
            allow_write=True,
        )
    ]

    def client_factory(task):
        return FakeClient(
            [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_apply",
                            "name": "apply_patch",
                            "input": {"patch": patch, "summary": "change example"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
                {
                    "content": [{"type": "text", "text": f"completed {task.id}"}],
                    "stop_reason": "end_turn",
                },
            ]
        )

    result = run_batch_tasks(
        tasks=tasks,
        out_dir=out_dir,
        parallel=1,
        client_factory=client_factory,
    )

    assert result.status == "completed"
    assert sample.read_text(encoding="utf-8") == "new\n"


def test_load_batch_tasks_sanitizes_ids_and_uses_default_root(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    root = tmp_path / "root"
    root.mkdir()
    tasks_file.write_text(
        json.dumps(
            [
                {
                    "id": "Task A/1",
                    "task": "Inspect",
                    "owns": ["src/a.py"],
                    "allow_write": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    tasks = load_batch_tasks(tasks_file=tasks_file, default_root=root)

    assert tasks[0].id == "Task-A-1"
    assert tasks[0].root == root
    assert tasks[0].owns == ["src/a.py"]
    assert tasks[0].allow_write is True


def test_validate_batch_ownership_rejects_duplicate_owned_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tasks = [
        BatchTask(id="task-a", task="A", root=root, owns=["src/app.py"]),
        BatchTask(id="task-b", task="B", root=root, owns=["./src/app.py"]),
    ]

    with pytest.raises(ValueError, match="ownership conflict"):
        validate_batch_ownership(tasks)


def test_validate_batch_ownership_rejects_parent_child_overlaps(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tasks = [
        BatchTask(id="task-a", task="A", root=root, owns=["src"]),
        BatchTask(id="task-b", task="B", root=root, owns=["src/app.py"]),
    ]

    with pytest.raises(ValueError, match="ownership conflict"):
        validate_batch_ownership(tasks)

    reversed_tasks = [
        BatchTask(id="task-a", task="A", root=root, owns=["src/app.py"]),
        BatchTask(id="task-b", task="B", root=root, owns=["src"]),
    ]

    with pytest.raises(ValueError, match="ownership conflict"):
        validate_batch_ownership(reversed_tasks)


def test_run_batch_tasks_writes_owner_map(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "batch-runs"
    tasks = [
        BatchTask(id="task-a", task="Inspect A", root=root, owns=["src/a.py"]),
        BatchTask(id="task-b", task="Inspect B", root=root, owns=["src/b.py"]),
    ]

    def client_factory(task):
        return FakeClient(
            [
                {
                    "content": [{"type": "text", "text": f"completed {task.id}"}],
                    "stop_reason": "end_turn",
                }
            ]
        )

    result = run_batch_tasks(
        tasks=tasks,
        out_dir=out_dir,
        parallel=1,
        client_factory=client_factory,
    )

    owner_map = json.loads((result.out_dir / "ownership.json").read_text(encoding="utf-8"))
    assert owner_map["src/a.py"] == "task-a"
    assert owner_map["src/b.py"] == "task-b"


def test_run_batch_dry_run_rejects_ownership_conflicts(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            [
                {"id": "task-a", "task": "A", "owns": ["shared.txt"]},
                {"id": "task-b", "task": "B", "owns": ["./shared.txt"]},
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "run-batch",
            "--tasks-file",
            str(tasks_file),
            "--default-root",
            str(root),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ownership conflict" in captured.err


def test_create_isolated_worktree_creates_branch_and_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required for worktree isolation")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    (repo / "example.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "example.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "test: initial",
        ],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "worktrees"

    result = create_isolated_worktree(
        repo_root=repo,
        worktree_base=base,
        task_id="Task A",
    )

    assert result.status == "created"
    assert result.path.exists()
    assert (result.path / "example.txt").read_text(encoding="utf-8") == "base\n"
    assert result.branch.startswith("mmx-worker-Task-A-")


def test_worker_only_persists_tool_use_content_from_assistant_response(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    sample = root / "report.md"
    sample.write_text("agent loop evidence\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    fake_client = FakeClient(
        [
            {
                "content": [
                    {"type": "thinking", "thinking": "internal notes"},
                    {
                        "type": "tool_use",
                        "id": "call_read",
                        "name": "read_file",
                        "input": {"path": str(sample)},
                    },
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        ]
    )
    worker = MmxWorker(
        config=WorkerConfig(root=root, out_dir=out_dir, max_steps=3),
        client=fake_client,
    )

    result = worker.run("Inspect without persisting internal thinking.")

    final_messages = json.loads(result.messages_path.read_text(encoding="utf-8"))
    assert final_messages[1]["content"] == [
        {
            "type": "tool_use",
            "id": "call_read",
            "name": "read_file",
            "input": {"path": str(sample)},
        }
    ]


def test_resolve_executable_uses_pathext_for_windows_cmd_shims(tmp_path, monkeypatch):
    shim = tmp_path / "fake-mmx.CMD"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")

    assert _resolve_executable("fake-mmx") == str(shim)


def test_mmx_client_retries_transient_network_failures(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                6,
                stdout="",
                stderr='{"error":{"message":"Network request failed."}}',
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"content":[{"type":"text","text":"ok"}]}',
            stderr="",
        )

    monkeypatch.setattr("mmx_worker_bridge.core.subprocess.run", fake_run)

    config = WorkerConfig(
        root=tmp_path,
        out_dir=tmp_path,
        max_retries=2,
        retry_base_seconds=0,
    )
    client = MmxCliClient(config=config, run_dir=tmp_path)

    response = client.complete(
        messages=[{"role": "user", "content": "hello"}],
        tool_paths=[],
        system_prompt="system",
    )

    assert extract_text(response) == "ok"
    assert len(calls) == 2
