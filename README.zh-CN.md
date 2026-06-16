# mmx-worker-bridge

[English](README.md) | 中文

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-24%20passed-brightgreen?logo=pytest)](tests/)
[![Code Style](https://img.shields.io/badge/code%20style-dataclass-blue)](https://docs.python.org/3/library/dataclasses.html)

**沙箱化多智能体编排桥接器** — 将有界编码任务委派给 MiniMax 作为外部 worker，具备主智能体审核门控、git worktree 隔离和基于 patch 的代码审查。

![架构图](docs/assets/architecture.svg)

---

## 摘要

现代 AI 编码助手（Claude Code、Codex CLI）作为主智能体表现出色，但在多提供商凭据管理和自主执行可靠性方面面临挑战。`mmx-worker-bridge` 通过将 MiniMax `mmx` CLI 包装为**沙箱化外部 worker**，实现主智能体编排模式来解决这一问题。

系统实现了**审核门控架构**：worker 在根目录作用域沙箱中运行，使用受限工具（文件读取、搜索、glob 发现、只读命令），生成可审核的产物（`result.md`、`run.jsonl`、`proposed_patches/`），只有通过主智能体批准的 patch 工作流才能应用更改。写入操作需要显式的 git worktree 隔离、路径所有权声明和 `git apply --check` 验证。

**核心洞察**：MiniMax 在有界子智能体任务中展现出强大的指令遵循能力，使其更适合作为委派 worker 而非主会话智能体。本桥接器利用这一优势，同时在每个写入边界保持人在回路的审核。

---

## 特性

| 类别 | 能力 |
|------|------|
| **沙箱执行** | 根目录作用域文件操作、shell 注入防护、路径遍历保护 |
| **审核门控** | 所有 patch 在应用前需主智能体批准 |
| **Git Worktree 隔离** | 写入操作在隔离分支中进行，永不触及主工作树 |
| **路径所有权** | 批量任务声明拥有的路径；重叠所有权被拒绝 |
| **智能体工具循环** | 多步工具使用循环：`read_file`、`rg_search`、`list_dir`、`glob_find`、`run_command`、`propose_patch`、`apply_patch`、`final_answer` |
| **批量执行** | 并行任务执行，冲突检测和 dry-run 验证 |
| **指数退避重试** | 对瞬态 `mmx` CLI 故障进行指数退避重试 |
| **产物协议** | 结构化输出：`result.md`、`run.jsonl`、`proposed_patches/*.diff`、`batch.summary.json` |

---

## 工作原理

### 智能体循环

```
┌─────────────────────────────────────────────────────────────┐
│  主智能体 (Claude / Codex)                                    │
│  ├─ 委派有界任务                                              │
│  ├─ 审核产物 (result.md, patches, git diff)                  │
│  └─ 决策：应用 / 拒绝 / 要求修改                               │
└──────────────────────────────┬──────────────────────────────┘
                               │ task prompt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  mmx-worker-bridge                                          │
│  ├─ 构建带工具 schema 的 system prompt                       │
│  ├─ 调用 mmx text chat --messages-file                      │
│  ├─ 解析 tool_use 响应                                       │
│  ├─ 在沙箱中执行工具 (ReadOnlyTools)                          │
│  ├─ 将 tool_result 反馈给 mmx                                │
│  └─ 循环直到 final_answer 或 max_steps                       │
└──────────────────────────────┬──────────────────────────────┘
                               │ mmx CLI
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  MiniMax API (凭据由 mmx auth 管理)                           │
└─────────────────────────────────────────────────────────────┘
```

### 写入边界

| 模式 | 可用工具 | 写入权限 |
|------|---------|---------|
| **默认（只读）** | `read_file`、`rg_search`、`list_dir`、`glob_find`、`run_command`、`propose_patch`、`final_answer` | 无 — patch 保存为产物 |
| **受控写入** | 以上全部 + `apply_patch` | 仅在 git worktree 中，仅对 `--owns` 路径，仅在 `git apply --check` 通过后 |

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
│   └── test_bridge.py           # 24 个测试，覆盖安全性、patch、批量、worktree
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

## 技术亮点

- **零外部依赖** — 纯 Python 3.11+ 标准库（`dataclasses`、`subprocess`、`concurrent.futures`、`pathlib`）
- **面向协议设计** — `CompletionClient` 协议支持 mock 测试和替代后端
- **安全优先的工具沙箱** — `run_command` 拒绝 shell 元字符（`&|;<>`\`()`）、根目录外路径和写入型命令
- **Patch 验证流水线** — unified diff 解析 → 根目录范围检查 → 所有权检查 → `git apply --check` → `git apply`
- **批量所有权冲突检测** — 规范化路径并在执行前检测重叠
- **Windows 兼容** — 通过 `PATHEXT` 解析处理 `.CMD`/`.BAT` shim

---

## 环境要求

- Python 3.11 或更新版本
- Git
- 已安装并登录 MiniMax `mmx` CLI

```powershell
mmx auth
```

## 安装

```powershell
git clone git@github.com:eeljoe/mmx-worker-bridge.git
cd mmx-worker-bridge
pip install -e .
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

`mmx-worker-bridge` 对所有写入操作强制执行**主智能体审核门控**：

1. **默认模式为只读** — `run_command` 拒绝 shell 语法、写入命令和根目录外路径
2. **Patch 作为产物** — `propose_patch` 保存 diff 供审阅，永不直接应用
3. **写入模式需要隔离** — `apply_patch` 仅在 git worktree 中工作，需 `--allow-write` 和 `--owns` 声明
4. **每个 patch 都经过验证** — unified diff 格式 → 根目录范围 → 所有权 → `git apply --check` → `git apply`

Worker 无法修改其声明范围外的文件。主智能体（Claude、Codex 或人类）在合并前检查所有产物。

---

## 测试

```powershell
pip install pytest
pytest tests/
```

测试覆盖包括：
- 根目录范围强制执行（路径遍历拒绝）
- Shell 注入防护
- Patch 验证和所有权检查
- 批量所有权冲突检测
- Git worktree 隔离
- 指数退避重试
- 使用 mock 客户端的工具循环执行

---

## 状态

项目仍处于早期抽取阶段。第一个稳定版本前 API 可能调整。

---

## 许可证

MIT © 2026 mmx-worker-bridge contributors
