# mmx-worker-bridge

[English](README.md) | 中文

面向 Codex 和 Claude 工作流的 MiniMax 外部编码 worker。

`mmx-worker-bridge` 把 MiniMax 的 `mmx` CLI 包装成一个可控的外部 worker。主 agent 可以把有边界的任务交给 MMX worker，随后检查产物、审阅 patch、运行测试，再决定是否应用或合并。

![架构图](docs/assets/architecture.svg)

## 为什么需要它

在 Claude Code 和 Codex CLI 里做多 provider / 多凭据管理很麻烦。除非额外做一个中转服务来聚合 API key，否则对 MiniMax 来说，更简单的方案是直接让已经登录的 `mmx` CLI 管理 MiniMax 凭据，再把它包装成外部 worker。

MiniMax 也更适合作为 sub-agent，而不是 main session agent。它做完整自主开发时错误率偏高，但指令遵循性很好，benchmark 和实际表现都更适合做有边界、可审核、可并发的 delegated worker。

这个项目围绕这个定位设计：

- MiniMax 凭据留在 `mmx` CLI，不塞进 Claude Code 或 Codex。
- MiniMax 作为外部 worker 处理有边界任务，而不是主会话 agent。
- worker 有足够工具：读文件、搜索、列目录、找文件、运行受限只读命令。
- worker 产出可审核 artifact：`result.md`、`run.jsonl`、`proposed_patches/`、`git diff` 和测试结果。
- 最终由 Codex 或 Claude 作为 lead agent 审阅并决定什么能落地。

## 环境要求

- Python 3.11 或更新版本。
- Git。
- 已安装并登录 MiniMax `mmx` CLI。

```powershell
mmx auth
```

## 从源码安装

```powershell
git clone <repo-url> mmx-worker-bridge
cd mmx-worker-bridge
python -m pip install -e .
```

## 快速开始

运行只读 worker：

```powershell
mmx-worker-bridge --task "Inspect this repository and summarize the test strategy." --root "<project-root>" --out-dir "mmx-subagent-runs"
```

审阅 worker 产出的 patch：

```powershell
mmx-worker-bridge review-patch --root "<project-root>" --patch "<patch.diff>"
```

为实现任务创建隔离 worktree：

```powershell
mmx-worker-bridge create-worktree --root "<git-root>" --worktree-base "<worktree-dir>" --task-id "docs-update"
```

在隔离 worktree 中运行受控写入：

```powershell
mmx-worker-bridge --task "Update docs for the new option." --root "<worktree-path>" --allow-write --owns "docs"
```

运行批量任务前先 dry run：

```powershell
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --dry-run
```

## 安全模型

![审核门](docs/assets/review-gate.svg)

`run_command` 不是任意 shell。它会拒绝 shell 语法、写入型命令和 root 外路径。文件修改默认通过 patch artifact，或者在隔离 worktree 中通过受控 `apply_patch` 完成。

## 文档

- [Codex 接入](docs/codex.md)
- [Claude 工作流](docs/claude.md)
- [安全模型](docs/safety.md)
- [示例](docs/examples.md)
- [给 agent 读取的安装说明](Install.md)

## 状态

项目仍处于早期抽取阶段。第一个稳定版本前 API 可能调整。
