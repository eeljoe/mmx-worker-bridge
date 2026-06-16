# mmx-worker-bridge

[English](README_en.md) | 中文

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-31%20passed-brightgreen?logo=pytest)](tests/)
[![Code Style](https://img.shields.io/badge/code%20style-dataclass-blue)](https://docs.python.org/3/library/dataclasses.html)

**沙箱化多智能体编排桥接器** — 将有界编码任务委派给 MiniMax 作为外部 worker，具备主智能体审核门控、git worktree 隔离和基于 patch 的代码审查。

![架构图](docs/assets/architecture.svg)

---

## ⚠️ 写在前面：这个项目的背景

> **先说结论**：现在有了 **Pi Agent**，这个项目其实没太大实际用途了。
>
> 当时写这个是因为 MiniMax 按**调用次数**收费，API 调用量很大，我就想让 MiniMax 当个"小工"——专门干那些屎山代码清理、批量重构、枯燥重复的简单活。这种"主智能体指挥 + 外部小工干活"的模式在当时挺合理的。
>
> 但现在情况变了：
> 1. **Pi Agent** 出来了，搭多智能体系统更快更好，直接用 Pi Agent 就行
> 2. **MiniMax 改成按 tokens 收费**了，不再是按次数，原来"次数多=便宜"的优势没了
> 3. 实际开发中，直接上 Pi Agent 的效果比这个桥接方案好太多
>
> **那为什么还留着这个项目？**
> - 代码里的一些设计思路还是可以看看的，比如路径沙箱、patch 验证、所有权冲突检测这些
> - 可以了解一下从"按次数计费"到"按 tokens 计费"对架构选择的影响
> - 就当个学习案例吧，看看多智能体编排是怎么做的
>
> 如果你现在要做类似的东西，建议直接用 Pi Agent 或者其他现代多智能体框架，别用我这个。

---

## 项目简介

简单来说，这个项目就是把 MiniMax 的 `mmx` CLI 包装成一个**沙箱化的外部 worker**。

背景是 Claude Code、Codex CLI 这些 AI 编码助手当主智能体挺好用的，但多 provider 的凭据管理很麻烦，而且让 AI 完全自主执行也有风险。所以我就做了这个桥接器：

- Worker 在限定的目录范围内运行，只能用一些受限的工具（读文件、搜索、跑只读命令）
- 每次操作都会生成可审核的产物（`result.md`、`run.jsonl`、`proposed_patches/`）
- 要改文件必须走 patch 流程，主智能体审核通过后才能应用
- 写入操作必须在 git worktree 里进行，还要声明拥有哪些路径，`git apply --check` 通过才行

**核心想法**：MiniMax 做有边界的子任务其实挺靠谱的，指令遵循性很好，适合当被委派的 worker，而不是让它当主会话智能体。这个桥接器就是利用这一点，同时在每个写入边界都保留人在回路的审核。

---

## 主要功能

| 功能 | 说明 |
|------|------|
| **沙箱执行** | 文件操作限制在指定目录内，防止 shell 注入和路径遍历 |
| **审核门控** | 所有代码修改都要主智能体批准才能应用 |
| **Git Worktree 隔离** | 写入操作在独立分支进行，不会动主工作树 |
| **路径所有权** | 批量任务要声明拥有哪些路径，有冲突会报错 |
| **工具循环** | 支持多步操作：读文件、搜索、列目录、跑命令、提 patch、给最终答案 |
| **批量执行** | 可以并行跑多个任务，有冲突检测和 dry-run |
| **失败重试** | mmx 命令失败会自动重试，指数退避 |
| **产物输出** | 每次运行都会生成 `result.md`、`run.jsonl`、patch 文件等 |

---

## 怎么工作的

### 智能体循环

```
┌─────────────────────────────────────────────────────────────┐
│  主智能体 (Claude / Codex)                                    │
│  ├─ 把任务交给 worker                                        │
│  ├─ 看产物 (result.md, patches, git diff)                    │
│  └─ 决定：应用 / 拒绝 / 让 worker 改                          │
└──────────────────────────────┬──────────────────────────────┘
                               │ task prompt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  mmx-worker-bridge                                          │
│  ├─ 构建 system prompt，告诉 worker 有哪些工具                │
│  ├─ 调用 mmx text chat                                       │
│  ├─ 解析 tool_use 响应                                       │
│  ├─ 在沙箱里执行工具                                          │
│  ├─ 把结果反馈给 mmx                                         │
│  └─ 循环，直到 worker 说 final_answer 或者达到最大步数          │
└──────────────────────────────┬──────────────────────────────┘
                               │ mmx CLI
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  MiniMax API (凭据由 mmx auth 管理)                           │
└─────────────────────────────────────────────────────────────┘
```

### 写入权限

| 模式 | 能用的工具 | 能写文件吗 |
|------|-----------|-----------|
| **默认（只读）** | 读文件、搜索、列目录、跑只读命令、提 patch、给答案 | 不能 — patch 只是保存下来当产物 |
| **受控写入** | 上面全部 + apply_patch | 只能在 git worktree 里，只能写声明拥有的路径，还要 `git apply --check` 通过 |

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

- **零外部依赖** — 纯 Python 3.11+ 标准库，就用了 `dataclasses`、`subprocess`、`concurrent.futures`、`pathlib` 这些
- **面向协议设计** — `CompletionClient` 是个 Protocol，方便 mock 测试，也能换别的后端
- **安全优先** — `run_command` 会拒绝 shell 元字符（`&|;<>`\`()`）、根目录外的路径、写入型命令
- **Patch 验证流程** — 解析 unified diff → 检查是不是在根目录内 → 检查所有权 → `git apply --check` → 真的 apply
- **批量冲突检测** — 跑之前会检查路径所有权有没有重叠
- **Windows 兼容** — 能处理 `.CMD`/`.BAT` 这种 shim，用 `PATHEXT` 解析

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

`mmx-worker-bridge` 对所有写入操作都强制走**主智能体审核**：

1. **默认只读** — `run_command` 会拒绝 shell 语法、写入命令、根目录外的路径
2. **Patch 只是产物** — `propose_patch` 只是把 diff 存下来，不会直接应用
3. **写入要隔离** — `apply_patch` 只能在 git worktree 里用，还要 `--allow-write` 和 `--owns` 声明
4. **每个 patch 都要验证** — unified diff 格式 → 根目录范围 → 所有权 → `git apply --check` → 才真的 apply

Worker 改不了它声明范围外的文件。主智能体（Claude、Codex 或者人）合并前会检查所有产物。

---

## 测试

```powershell
pip install pytest
pytest tests/
```

31 个测试，覆盖了：
- 根目录范围限制（路径遍历会被拒绝）
- Shell 注入防护
- Patch 验证和所有权检查
- 批量所有权冲突检测
- Git worktree 隔离
- 指数退避重试
- 用 mock 客户端的工具循环执行

---

## 状态

从本地原型刚抽出来，API 可能还会变。

---

## 许可证

MIT © 2026 mmx-worker-bridge contributors
