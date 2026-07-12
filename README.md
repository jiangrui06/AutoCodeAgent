<div align="center">

# AutoCodeAgent — 自动编码调试智能体

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green)](https://github.com/langchain-ai/langgraph)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**输入自然语言需求 → Agent 自动完成规划、编码、执行、调试、修复全流程**

</div>

---

## 目录

- [项目简介](#项目简介)
- [核心特性](#核心特性)
- [适用场景](#适用场景)
- [项目架构](#项目架构)
- [快速开始](#快速开始)
- [使用示例](#使用示例)
- [安全机制](#安全机制)
- [可观测性](#可观测性)
- [配置文件](#配置文件)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [技术栈](#技术栈)
- [License](#license)

---

## 项目简介

**AutoCodeAgent** 是一个基于 **LangGraph** 状态机驱动的自动编码调试智能体。只需输入一句**自然语言开发需求**，Agent 就会自动完成以下闭环：

```
需求分析(Planner) → 代码生成(Coder) → 静态检查(Linter) → 安全扫描(Scanner)
                                                        ↓
                                              沙箱执行(Executor)
                                                        ↓
                                  无错 → 结束     有错 → 自动修复(Fixer) → 重新执行
```

全流程无需人工干预，最多自动修复 **5 轮**，每轮代码自动快照存档。

---

## 核心特性

| 特性 | 说明 |
| --- | --- |
| 🤖 **全自动闭环** | 规划 → 编码 → 检查 → 执行 → 调试 → 修复，无需介入 |
| 🔍 **静态代码检查** | 执行前先用 `pyflakes` 抓语法错误、未定义变量，省 Token 省时间 |
| 🔒 **安全沙箱** | 独立子进程隔离执行，超时强杀，Windows 下低优先级运行不卡机 |
| 🛡️ **代码扫描** | AST + 正则双引擎静态分析，拦截危险代码（文件删除、网络外泄、挖矿等） |
| 🔄 **自动修复** | 分析报错堆栈 → 修复 → 重新执行，最多 5 轮 |
| 🖥️ **双模式交互** | 命令行 CLI + Gradio Web 可视化界面 |
| 📊 **可观测性** | 内置 Loguru 日志 + 可选 LangSmith 追踪，每一步都有迹可循 |
| 💾 **版本快照** | 每次重试的代码自动保存，完整可追溯 |

---

## 适用场景

- **快速原型验证** — 用自然语言描述想法，秒级获得可运行代码
- **学习辅助** — 观察 LLM 如何从需求到代码再到调试的全过程
- **自动化编程** — 作为 CI/CD 管道的一环，自动生成特定功能脚本
- **教学演示** — 展示 Agent 驱动的自动编程工作流

---

## 项目架构

### 工作流

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Planner   │────▶│    Coder    │────▶│   Linter    │────▶│   Scanner   │
│  (需求分析)  │     │  (代码生成)  │     │ (静态检查)  │     │ (安全扫描)  │
└─────────────┘     └─────────────┘     └─────────────┘     └──────┬──────┘
                                                                    │
                                                                    ▼
                                                           ┌─────────────┐
                                                           │   Executor  │
                                                           │  (沙箱执行)  │
                                                           └──────┬──────┘
                                                                  │
                                                                  ▼
                                                           ┌─────────────┐
                                                           │    Judge    │
                                                           │  (路由判断)  │
                                                           └──────┬──────┘
                                            ┌────────────────────┐
                                            │                    │
                                            ▼                    ▼
                                     ┌─────────────┐      ╔═════════════╗
                                     │    Fixer    │      ║     END     ║
                                     │  (自动修复)  │      ║  (任务完成)  ║
                                     └──────┬──────┘      ╚═════════════╝
                                            │ (回到 Executor)
                                            ▼
                                     ┌─────────────┐
                                     │   Executor  │
                                     └─────────────┘
```

### 模块说明

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| **配置中心** | [`config.py`](AutoCodeAgent/config.py) | Pydantic Settings 统一管理 `.env`，支持 LangSmith 等第三方库自动读取 |
| **LLM 客户端** | [`llm_client.py`](AutoCodeAgent/llm_client.py) | 封装 ChatOpenAI，支持 SiliconFlow / 商汤 / OpenAI 等兼容接口 |
| **状态模型** | [`state_model.py`](AutoCodeAgent/state_model.py) | Pydantic v2 全局状态定义，贯穿全流程 |
| **图节点** | [`graph_nodes.py`](AutoCodeAgent/graph_nodes.py) | Planner → Coder → Executor → Judge → Fixer 节点逻辑 |
| **图调度** | [`graph_builder.py`](AutoCodeAgent/graph_builder.py) | LangGraph StateGraph 组装，编排闭环流转 |
| **静态检查** | [`code_linter.py`](AutoCodeAgent/code_linter.py) | pyflakes 语义检查 + 编译语法检查 |
| **代码沙箱** | [`code_sandbox.py`](AutoCodeAgent/code_sandbox.py) | 子进程隔离执行，超时强杀，Windows 低优先级 |
| **安全扫描** | [`code_scanner.py`](AutoCodeAgent/code_scanner.py) | AST + 正则双引擎扫描，检测危险操作 |
| **文件工具** | [`file_util.py`](AutoCodeAgent/file_util.py) | 生成代码自动持久化，按时间戳/迭代轮次保存 |
| **日志** | [`logger.py`](AutoCodeAgent/logger.py) | Loguru 结构化日志，控制台 + 文件双输出 |
| **命令行入口** | [`main.py`](AutoCodeAgent/main.py) | CLI 交互，接收需求并运行完整流程 |
| **Web 界面** | [`app_web.py`](AutoCodeAgent/app_web.py) | Gradio 可视化界面，步骤级流式输出 |

---

## 快速开始

### 环境要求

- Python **3.10** 或更高版本
- 一个兼容 **OpenAI 接口** 的 LLM API（默认 [硅基流动 SiliconFlow](https://siliconflow.cn/)，已验证支持 [商汤 SenseNova](https://platform.sensenova.cn/)）

### 1. 安装依赖

```bash
cd AutoCodeAgent
pip install -r requirements.txt
```

### 2. 配置 API

```bash
# 复制环境变量模板
cp .env.example .env
```

编辑 `.env` 文件，填入你的 API 信息：

```ini
# SiliconFlow（硅基流动）配置
SILICONFLOW_API_KEY=sk-your-api-key-here
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V4-Pro

# 商汤 SenseNova 配置示例
# SILICONFLOW_BASE_URL=https://token.sensenova.cn/v1
# SILICONFLOW_MODEL=deepseek-v4-flash

# LLM 超参（可选，有默认值）
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=8192
LLM_TIMEOUT=300

# 沙箱超时（秒，可选，默认 15）
SANDBOX_TIMEOUT=15
```

> **支持其他 LLM 服务？** 只需修改 `SILICONFLOW_BASE_URL` 和 `SILICONFLOW_MODEL` 即可兼容任何 OpenAI 兼容接口。

### 3. 运行

#### 方式一：命令行（CLI）

```bash
python main.py
```

交互输入需求：

```
需求 > 实现一个学生成绩管理系统，支持添加学生、查询成绩、计算平均分、删除学生
```

或直接传参：

```bash
python main.py "写一个计算器程序，支持加减乘除、历史记录"
```

#### 方式二：Web 可视化界面（推荐）

```bash
python app_web.py
```

浏览器自动打开 `http://localhost:7870`，输入需求即可实时观察每一步结果。

---

## 使用示例

### 示例 1：学生成绩管理系统

**输入需求：**
> 实现一个学生成绩管理系统，支持添加学生、查询成绩、计算平均分、删除学生数据

**执行流程：**
1. Planner 输出系统设计方案
2. Coder 生成 `Student` / `GradeManager` 完整代码
3. Linter 快速静态检查
4. Scanner 安全扫描
5. Executor 运行演示用例
6. 如有报错，Fixer 自动修复，最多 5 轮
7. 最终生成可运行的 Python 脚本并保存

### 示例 2：批量文件重命名工具

**输入需求：**
> 写一个批量文件重命名工具，支持递归处理子目录，支持正则替换和序号编号两种模式

全流程同上，自动生成带 `argparse` 命令行参数的文件操作工具。

---

## 安全机制

### 1. 静态代码检查

[`code_linter.py`](AutoCodeAgent/code_linter.py) 在沙箱执行前先做两件事：

- `compile()` 语法检查 — 语法错误直接拦截
- `pyflakes` 语义检查 — 未定义变量等直接拦截

### 2. 静态代码扫描

[`code_scanner.py`](AutoCodeAgent/code_scanner.py) 使用 AST + 正则双引擎检测：

| 风险等级 | 检测项 |
| --- | --- |
| 🔴 严重 | 动态代码执行 (`eval`/`exec`)、系统命令调用、文件删除、base64 混淆载荷、注册表持久化 |
| 🟠 高危 | 网络连接（socket/requests）、`subprocess` 模块、可疑恶意命名、明显无限循环 |
| 🟡 可疑 | 文件读取、超长混淆行、环境变量读取、加密库导入、大范围循环 |
| 🔵 提示 | 常规的 `hashlib` 使用等 |

### 3. 子进程隔离执行

[`code_sandbox.py`](AutoCodeAgent/code_sandbox.py) 使用 `subprocess.run()` 在独立进程中执行：

- **进程级隔离** — 死循环不阻塞主程序，崩溃不影响主进程
- **超时强杀** — 默认 15 秒超时，超时后 OS 级终止
- **低优先级** — Windows 下使用 `BELOW_NORMAL_PRIORITY_CLASS`，避免卡死整机
- **隔离模式** — `python -I` 隔离模式，不加载用户 site-packages

### 4. 强制运行标记

如果代码被安全扫描误拦截，可在需求末尾加上标记强制运行：

```
[我已检查，强制运行]
```

---

## 可观测性

### 日志

项目已接入 [Loguru](https://github.com/Delgan/loguru)：

- 控制台彩色输出，带时间、级别、模块、行号
- 文件日志自动按天轮转，保留 7 天，路径：`logs/autocode-agent.log`

### LangSmith 追踪

如需在 [LangSmith](https://smith.langchain.com/) 上追踪 Agent 每一步的输入输出，在 `.env` 中开启：

```ini
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=你的_langsmith_key
LANGCHAIN_PROJECT=autocode-agent
```

> 配置在应用启动时自动加载，无需改代码。

---

## 配置文件

### `.env` 完整选项

```ini
# ── LLM 服务商配置（必需） ──
SILICONFLOW_API_KEY=sk-xxx
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V4-Pro

# ── LLM 超参（可选） ──
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=8192
LLM_TIMEOUT=300

# ── 沙箱（可选） ──
SANDBOX_TIMEOUT=15

# ── LangSmith 可观测性（可选） ──
LANGCHAIN_TRACING_V2=false
LANGCHAIN_API_KEY=
LANGCHAIN_PROJECT=autocode-agent
```

---

## 项目结构

```
AutoCodeAgent/
├── main.py                  # 命令行入口
├── app_web.py               # Gradio Web 可视化界面
├── config.py                # Pydantic Settings 配置中心
├── llm_client.py            # LLM 客户端封装
├── state_model.py           # LangGraph 全局状态模型
├── graph_nodes.py           # 流程节点逻辑
├── graph_builder.py         # LangGraph 图调度组装
├── code_linter.py           # 静态代码检查
├── code_sandbox.py          # 安全子进程执行沙箱
├── code_scanner.py          # 代码安全扫描器
├── file_util.py             # 代码持久化工具
├── logger.py                # Loguru 日志配置
├── requirements.txt         # Python 依赖列表
├── setup.py                 # 安装脚本
├── .env.example             # 环境变量模板
├── .gitignore               # Git 忽略规则
├── logs/                    # 运行日志
└── auto_generated_code/     # LLM 生成的代码快照存档
    └── .gitkeep
```

---

## 常见问题

### Q: 启动报错 "LLM 配置不完整"

确保已复制 `.env.example` 为 `.env` 并填入正确的 API Key：

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Key
```

### Q: 报错 `no Route matched with those values` 或 `model is not found`

这是 Base URL 或模型名填错了。常见正确配置：

- **SiliconFlow**: `https://api.siliconflow.cn/v1`
- **商汤 SenseNova**: `https://token.sensenova.cn/v1`

模型名请填写你账号下有权限且实际存在的模型。

### Q: 报错余额不足 / PermissionDeniedError 403

这是 API 账户余额不足，与代码无关。请充值或更换可用的 API Key。

### Q: 代码执行一直超时或 CPU 100%

- 默认超时 15 秒，可在 `.env` 中调整 `SANDBOX_TIMEOUT`
- 检查生成的代码是否有死循环或无限等待
- Windows 下沙箱已自动降低子进程优先级，不会卡死整机

### Q: Web 界面无法打开

确保 Gradio 正确安装：

```bash
pip install -r requirements.txt
```

默认地址：`http://localhost:7870`

### Q: 如何切换不同的 LLM 模型？

修改 `.env` 中的 `SILICONFLOW_BASE_URL` 和 `SILICONFLOW_MODEL`：

- **DeepSeek 官方**：`https://api.deepseek.com/v1`
- **OpenAI**：`https://api.openai.com/v1`
- **商汤 SenseNova**：`https://token.sensenova.cn/v1`

### Q: 生成的代码保存在哪里？

所有代码自动保存在 `auto_generated_code/` 目录下：
- `iter_00_*.py` — 首版代码
- `iter_01_*.py` ~ `iter_05_*.py` — 各轮修复版本
- `code_*_final.py` — 最终结果

---

## 技术栈

| 技术 | 用途 |
| --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 状态机驱动的 Agent 工作流编排 |
| [LangChain](https://github.com/langchain-ai/langchain) | LLM 调用链与 Prompt 模板 |
| [ChatOpenAI](https://python.langchain.com/docs/integrations/chat/openai/) | 兼容 OpenAI 接口的 LLM 客户端 |
| [Pydantic v2](https://docs.pydantic.dev/) | 类型安全的状态模型与配置管理 |
| [Gradio](https://www.gradio.app/) | Web 可视化交互界面 |
| [Loguru](https://github.com/Delgan/loguru) | 结构化日志 |
| [pyflakes](https://github.com/PyCQA/pyflakes) | 静态代码检查 |

---

## License

MIT
