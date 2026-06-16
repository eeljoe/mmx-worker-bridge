# mmx-worker-bridge

[English](README_en.md) | 中文

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-31%20passed-brightgreen?logo=pytest)](tests/)
[![Code Style](https://img.shields.io/badge/code%20style-dataclass-blue)](https://docs.python.org/3/library/dataclasses.html)

**沙箱化多智能体编排桥接器** — 将有界编码任务委派给 MiniMax 作为外部 worker，具备主智能体审核门控、git worktree 隔离和基于 patch 的代码审查。

![架构图](docs/assets/architecture.svg)

---

## ⚠️ 项目背景

> **说明**：现在有了 **Pi Agent**，本项目的实用价值已经不大。
>
> 当时做这个是因为 MiniMax 按**调用次数**收费，调用量大，适合让 MiniMax 当外部 worker 处理批量任务——代码清理、批量重构、重复性工作。这种"主智能体 + 外部 worker"的模式在当时是合理的。
>
> 现在情况变了：
> 1. **Pi Agent** 出来了，搭多智能体系统更快更好
> 2. **MiniMax 改成按 tokens 收费**，原来"次数多=便宜"的优势没了
> 3. 实际开发中，直接用 Pi Agent 效果好很多
>
> **保留本项目的意义**：
> - 代码里的一些设计可以参考：路径沙箱、patch 验证、所有权冲突检测
> - 了解 API 计费模式（按次数 vs 按 tokens）对架构选择的影响
> - 作为多智能体编排的学习案例
>
> 如果你现在要做类似的东西，建议直接用 Pi Agent 或其他现代多智能体框架。

---

## 项目简介

本项目把 MiniMax 的 `mmx` CLI 包装成一个**沙箱化的外部 worker**。

Claude Code、Codex CLI 这些 AI 编码助手当主智能体很好用，但多 provider 凭据管理麻烦，让 AI 完全自主执行也有风险。这个桥接器解决了这个问题：

- Worker 在限定目录范围内运行，只能用受限工具（读文件、搜索、只读命令）
- 每次操作生成可审核的产物（`result.md`、`run.jsonl`、`proposed_patches/`）
- 改文件必须走 patch 流程，主智能体审核后才能应用
- 写入操作必须在 git worktree 里，要声明拥有哪些路径，`git apply --check` 通过才行

**核心思路**：MiniMax 做有边界的子任务很靠谱，指令遵循性好，适合当被委派的 worker，而不是主会话智能体。本项目利用这一点，同时在每个写入边界保留审核。

---

## 主要功能

| 功能 | 说明 |
|------|------|
| **沙箱执行** | 文件操作限制在指定目录，防止 shell 注入和路径遍历 |
| **审核门控** | 所有代码修改需主智能体批准才能应用 |
| **Git Worktree 隔离** | 写入操作在独立分支进行，不影响主工作树 |
| **路径所有权** | 批量任务声明拥有的路径，冲突会报错 |
| **工具循环** | 多步操作：读文件、搜索、列目录、跑命令、提 patch、返回结果 |
| **批量执行** | 并行任务，冲突检测和 dry-run |
| **失败重试** | mmx 失败自动重试，指数退避 |
| **产物输出** | 生成 `result.md`、`run.jsonl`、patch 文件等 |

---

## 工作原理

### 智能体循环

```
┌─────────────────────────────────────────────────────────────┐
│  主智能体 (Claude / Codex)                                    │
│  ├─ 委派任务给 worker                                        │
│  ├─ 审核产物 (result.md, patches, git diff)                  │
│  └─ 决定：应用 / 拒绝 / 要求修改                              │
└──────────────────────────────┬──────────────────────────────┘
                               │ task prompt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  mmx-worker-bridge                                          │
│  ├─ 构建 system prompt，定义可用工具                          │
│  ├─ 调用 mmx text chat                                       │
│  ├─ 解析 tool_use 响应                                       │
│  ├─ 在沙箱中执行工具                                          │
│  ├─ 将结果反馈给 mmx                                         │
│  └─ 循环直到 final_answer 或达到最大步数                       │
└──────────────────────────────┬──────────────────────────────┘
                               │ mmx CLI
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  MiniMax API (凭据由 mmx auth 管理)                           │
└─────────────────────────────────────────────────────────────┘
```

### 写入权限

| 模式 | 可用工具 | 写入权限 |
|------|---------|---------|
| **默认（只读）** | 读文件、搜索、列目录、只读命令、提 patch、返回结果 | 无 — patch 保存为产物 |
| **受控写入** | 以上全部 + apply_patch | 仅限 git worktree，仅限声明的路径，需 `git apply --check` 通过 |

---

## 项目结构

```
mmx-worker-bridge/
├── src/
│   └── mmx_worker_bridge/
│       ├── __init__.py          # 公共 API 导出
│       ├── cli.py               # CLI 入口点
│       ├── core.py              # Worker 循环、工具、批量、worktree (~1400 LOC)
│       ├── client.py            # mmx CLI 客户端封装
│       ├── worker.py            # MmxWorker 重导出
│       ├── batch.py             # 批量执行重导出
│       ├── worktree.py          # Git worktree 隔离重导出
│       ├── patch_review.py      # Patch 审查重导出
│       └── tools.py             # 工具 schema 定义
├── tests/
│   └── test_bridge.py           # 31 个测试，覆盖安全性、patch、批量、worktree
├── docs/
│   ├── assets/                  # 架构图 (SVG)
│   ├── safety.md                # 安全模型文档
│   ├── claude.md                # Claude Code 集成指南
│   ├── codex.md                 # Codex CLI 集成指南
│   ├── examples.md              # 使用示例
│   └── release.md               # 发布流程
├── examples/
│   ├── claude-workflow/         # AGENTS.md 策略块示例
│   └── codex-skill/             # Codex skill 配置
├── pyproject.toml               # PEP 621 项目元数据
├── LICENSE                      # MIT License
└── README.md                    # 本文件
```

---

## 技术细节

- **零外部依赖** — 纯 Python 3.11+ 标准库（`dataclasses`、`subprocess`、`concurrent.futures`、`pathlib`）
- **面向协议设计** — `CompletionClient` 是 Protocol，方便 mock 测试和替换后端
- **安全优先** — `run_command` 拒绝 shell 元字符（`&|;<>`\`()`）、根目录外路径、写入型命令
- **Patch 验证流程** — 解析 unified diff → 检查根目录范围 → 检查所有权 → `git apply --check` → 应用
- **批量冲突检测** — 执行前检查路径所有权是否重叠
- **Windows 兼容** — 通过 `PATHEXT` 解析处理 `.CMD`/`.BAT` shim

---

## 安装

### 快速安装（给 Agent 用）

直接复制下面这段给你的 agent：

```
Clone and install mmx-worker-bridge from https://github.com/eeljoe/mmx-worker-bridge, then follow the Install.md instructions to set it up.
```

或者更详细的版本：

```
1. git clone https://github.com/eeljoe/mmx-worker-bridge.git
2. cd mmx-worker-bridge
3. pip install -e .
4. 确保 mmx CLI 已安装并认证 (mmx auth)
5. 详细配置参考 Install.md
```

### 手动安装

```powershell
git clone git@github.com:eeljoe/mmx-worker-bridge.git
cd mmx-worker-bridge
pip install -e .
```

### 前置要求

- Python 3.11+
- Git
- MiniMax `mmx` CLI 已安装并认证

```powershell
mmx auth
```

## 快速开始

### 只读检查

```powershell
mmx-worker-bridge --task "检查此仓库并总结测试策略。" --root "<project-root>"
```

### Patch 提议（不写入）

```powershell
mmx-worker-bridge --task "提议一个 README 修复，不应用它。" --root "<project-root>"
```

### 审阅提议的 Patch

```powershell
mmx-worker-bridge review-patch --root "<project-root>" --patch "<patch.diff>"
```

### 受控实现（Git Worktree）

```powershell
# 1. 创建隔离 worktree
mmx-worker-bridge create-worktree --root "<git-root>" --worktree-base "<worktree-dir>" --task-id "docs-update"

# 2. 在 worktree 中以写入权限运行 worker
mmx-worker-bridge --task "更新新选项的文档。" --root "<worktree-path>" --allow-write --owns "docs"
```

### 批量执行

```powershell
# Dry-run 检查所有权冲突
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --dry-run

# 并行执行
mmx-worker-bridge run-batch --tasks-file "<tasks.json>" --parallel 2
```

---

## 文档

| 指南 | 描述 |
|------|------|
| [Codex 集成](docs/codex.md) | 将 mmx-worker-bridge 用作 Codex skill |
| [Claude 工作流](docs/claude.md) | 与 Claude Code 的主智能体工作流 |
| [安全模型](docs/safety.md) | 沙箱、审核门控、写入边界 |
| [示例](docs/examples.md) | 常见使用模式 |
| [Agent 安装指南](Install.md) | 机器可读的安装说明 |

---

## 安全性

![审核门控](docs/assets/review-gate.svg)

`mmx-worker-bridge` 对所有写入操作强制**主智能体审核**：

1. **默认只读** — `run_command` 拒绝 shell 语法、写入命令、根目录外路径
2. **Patch 作为产物** — `propose_patch` 保存 diff 供审阅，不直接应用
3. **写入需隔离** — `apply_patch` 仅在 git worktree 中，需 `--allow-write` 和 `--owns` 声明
4. **Patch 验证** — unified diff 格式 → 根目录范围 → 所有权 → `git apply --check` → 应用

Worker 无法修改声明范围外的文件。主智能体（Claude、Codex 或人）合并前检查所有产物。

---

## 测试

```powershell
pip install pytest
pytest tests/
```

31 个测试覆盖：
- 根目录范围限制（路径遍历拒绝）
- Shell 注入防护
- Patch 验证和所有权检查
- 批量所有权冲突检测
- Git worktree 隔离
- 指数退避重试
- Mock 客户端的工具循环执行

---

## 状态

从本地原型提取，API 可能还会调整。

---

## 许可证

MIT © 2026 mmx-worker-bridge contributors
