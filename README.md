# AutoCodeAgent — 自动编码调试智能体

## 项目简介

**AutoCodeAgent** 是一个基于 **LangGraph** 状态机驱动的自动编码调试智能体。你只需输入**自然语言开发需求**，Agent 就会全自动完成以下闭环流程：

1. **需求分析** — 理解需求，输出系统开发方案
2. **代码生成** — 根据方案生成完整可运行的 Python 代码
3. **安全执行** — 在独立子进程中隔离运行代码，捕获输出和报错
4. **自动修复** — 检测到报错后，自动分析错误并修复代码，最多重试 5 次

整个过程在 **LangGraph StateGraph** 中编排流转，无需人工干预。

### 适用场景

- 快速原型验证 — 用自然语言描述想法，秒级获得可运行代码
- 学习辅助 — 观察 LLM 如何从需求到代码再到调试的全过程
- 自动化编程 — 作为 CI/CD 管道的一环，自动生成特定功能脚本
- 教学演示 — 展示 Agent 驱动的自动编程工作流

### 核心特性

| 特性 | 说明 |
| --- | --- |
| 🤖 **全自动闭环** | 规划 → 编码 → 执行 → 调试 → 修复，无需人工介入 |
| 🔒 **安全沙箱** | 独立子进程隔离执行，超时强杀，避免死循环拖垮主进程 |
| 🛡️ **代码扫描** | AST + 正则双引擎静态分析，拦截危险代码（文件删除、网络外泄、挖矿等） |
| 🔄 **自动修复** | 内置调试器节点，分析报错堆栈 → 修复 → 重新执行，最多 5 轮 |
| 🖥️ **双模式交互** | 命令行 CLI + Gradio Web 可视化界面 |
| 💾 **版本快照** | 每次重试的代码自动保存，完整可追溯 |

---

## 项目架构

### 工作流

```
用户需求
    │
    ▼
┌──────────┐    ┌──────────┐    ┌──────────┐
│ Planner  │───▶│  Coder   │───▶│ Executor │
│ (方案规划) │    │ (代码生成) │    │ (沙箱执行) │
└──────────┘    └──────────┘    └──────────┘
                                    │
                                    ▼
                              ┌──────────┐
                              │  Judge   │
                              │ (路由判断) │
                              └────┬─────┘
                           ┌───────┴────────┐
                           ▼                 ▼
                      ┌──────────┐     ╔══════════╗
                      │  Fixer   │     ║   END    ║
                      │ (自动修复) │     ║ (任务完成) ║
                      └────┬─────┘     ╚══════════╝
                           │  (回到 Executor)
                           ▼
                      ┌──────────┐
                      │ Executor │  ← 重新执行修复后代码
                      └──────────┘
```

### 模块说明

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| **LLM 客户端** | `llm_client.py` | 封装 LLM 接口（默认 SiliconFlow DeepSeek-V4-Pro），支持 `.env` 配置 |
| **状态模型** | `state_model.py` | Pydantic v2 全局状态定义，贯穿全流程 |
| **图节点** | `graph_nodes.py` | 4 个核心节点 + 1 个路由函数：Planner → Coder → Executor → Judge → Fixer |
| **图调度** | `graph_builder.py` | LangGraph StateGraph 组装，编排闭环流转 |
| **代码沙箱** | `code_sandbox.py` | 子进程隔离执行，超时强杀（15 秒），捕获 stdout/stderr |
| **安全扫描** | `code_scanner.py` | AST + 正则双引擎扫描，检测危险操作和恶意模式 |
| **文件工具** | `file_util.py` | 生成代码自动持久化，按时间戳/迭代轮次保存 |
| **命令行入口** | `main.py` | CLI 交互，接收需求并运行完整流程 |
| **Web 界面** | `app_web.py` | Gradio 可视化界面，步骤级流式输出 |

---

## 快速开始

### 环境要求

- Python **3.10** 或更高版本
- 一个兼容 **OpenAI 接口** 的 LLM API（默认使用 [硅基流动 SiliconFlow](https://siliconflow.cn/)）

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

# LLM 超参（可选，有默认值）
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=8192
LLM_TIMEOUT=300
```

> **支持其他 LLM 服务？** 只需修改 `SILICONFLOW_BASE_URL` 和 `SILICONFLOW_MODEL` 即可兼容任何 OpenAI 兼容接口（如 OpenAI、DeepSeek 官方、LMSys 等）。

### 3. 运行

#### 方式一：命令行（CLI）

```bash
python main.py
```

然后在提示符后输入需求：

```
需求 > 实现一个学生成绩管理系统，支持添加学生、查询成绩、计算平均分、删除学生
```

也可以直接传参：

```bash
python main.py "写一个计算器程序，支持加减乘除、历史记录"
```

CLI 输出会显示完整的规划方案、代码生成、执行结果和修复过程。

#### 方式二：Web 可视化界面（推荐）

```bash
python app_web.py
```

浏览器会自动打开 `http://localhost:7870`。在输入框中输入需求，点击「自动生成并调试代码」即可实时观察每一步的中间结果。

---

## 使用示例

### 示例 1：学生成绩管理系统

**输入需求：**
> 实现一个学生成绩管理系统，支持添加学生、查询成绩、计算平均分、删除学生数据

**执行流程：**
1. Planner 输出系统设计方案（数据模型、模块拆分、API 设计）
2. Coder 生成包含 `Student` 类和 `GradeManager` 类的完整代码
3. Executor 运行代码的 `main()` 演示用例
4. 如有报错，Fixer 自动分析修复，最多 5 轮
5. 最终生成完整可运行的 Python 脚本

### 示例 2：文件批量重命名工具

**输入需求：**
> 写一个批量文件重命名工具，支持递归处理子目录，支持正则替换和序号编号两种模式

**执行流程：** 同上闭环流程，自动生成带 `argparse` 命令行参数的文件操作工具。

---

## 安全机制

AutoCodeAgent 内置多层安全防护：

### 1. 静态代码扫描（执行前）

`code_scanner.py` 使用 AST + 正则双引擎检测：

| 风险等级 | 检测项 |
| --- | --- |
| 🔴 严重 | 动态代码执行 (`eval`/`exec`)、系统命令调用、文件删除、base64 混淆载荷、注册表持久化 |
| 🟠 高危 | 网络连接（socket/requests）、`subprocess` 模块、可疑恶意命名 |
| 🟡 可疑 | 文件读取、超长混淆行、环境变量读取、加密库导入 |
| 🔵 提示 | 常规的 `hashlib` 使用等 |

### 2. 子进程隔离执行

`code_sandbox.py` 使用 `subprocess.run()` 在独立进程中执行代码：

- **进程级隔离** — 死循环不阻塞主程序，崩溃不影响主进程
- **超时强杀** — 默认 15 秒超时，超时后 OS 级终止
- **隔离模式** — `python -I` 隔离模式，不加载用户 site-packages

### 3. 强制运行标记

如果代码被安全扫描误拦截，可在需求末尾加上标记强制运行：

```
[我已检查，强制运行]
```

---

## 配置文件参考

### `.env` 完整选项

```ini
# ── LLM 服务商配置 ──
SILICONFLOW_API_KEY=sk-xxx          # API Key（必需）
SILICONFLOW_BASE_URL=https://...    # 接口地址（必需）
SILICONFLOW_MODEL=model-name        # 模型名称（必需）

# ── LLM 超参（可选） ──
LLM_TEMPERATURE=0.1                 # 生成温度，默认 0.1
LLM_MAX_TOKENS=8192                 # 最大 Token 数，默认 8192
LLM_TIMEOUT=300                     # API 请求超时（秒），默认 300
```

---

## 项目文件结构

```
AutoCodeAgent/
├── main.py                  # 命令行入口
├── app_web.py               # Gradio Web 可视化界面
├── llm_client.py            # LLM 客户端封装
├── state_model.py           # LangGraph 全局状态模型
├── graph_nodes.py           # 流程节点逻辑（规划/编码/执行/修复）
├── graph_builder.py         # LangGraph 图调度组装
├── code_sandbox.py          # 安全子进程执行沙箱
├── code_scanner.py          # 代码安全扫描器
├── file_util.py             # 代码持久化工具
├── requirements.txt         # Python 依赖列表
├── setup.py                 # 安装脚本
├── .env.example             # 环境变量模板
├── .gitignore               # Git 忽略规则
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

### Q: 代码执行一直超时

- 默认超时 15 秒，可在 `code_sandbox.py` 的 `safe_execute_code()` 中调整
- 检查生成的代码是否有死循环或无限等待
- 对于耗时操作，考虑简化需求

### Q: Web 界面无法打开

确保 Gradio 正确安装：

```bash
pip install gradio>=4.36.0
```

默认地址：`http://localhost:7870`

### Q: 如何切换不同的 LLM 模型？

修改 `.env` 中的配置即可：

- **DeepSeek 官方**：`SILICONFLOW_BASE_URL=https://api.deepseek.com/v1`
- **OpenAI**：`SILICONFLOW_BASE_URL=https://api.openai.com/v1`
- **LMSys**：`SILICONFLOW_BASE_URL=https://api.lmsys.org/v1`

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
| [Pydantic v2](https://docs.pydantic.dev/) | 类型安全的状态模型 |
| [Gradio](https://www.gradio.app/) | Web 可视化交互界面 |

---

## License

MIT
