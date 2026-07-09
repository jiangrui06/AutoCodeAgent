"""AutoCodeAgent Gradio 网页交互入口

可视化界面，输入需求一键运行 Agent，实时查看每轮调试过程。

启动：
    python app_web.py
"""

import gradio as gr

from file_util import get_all_generated_files, save_code_to_file
from graph_nodes import coder_node, executor_node, fixer_node, judge_route, planner_node
from logger import logger
from state_model import CodeAgentState

# ── 常量 ──
SEPARATOR = "-" * 50


def _apply_updates(state: CodeAgentState, updates: dict) -> CodeAgentState:
    """将节点返回的 dict（部分更新）应用到状态对象"""
    for key, value in updates.items():
        setattr(state, key, value)
    return state


def format_output(state: CodeAgentState) -> str:
    """将最终状态格式化为可读文本"""
    stdout = state.exec_stdout or "（无输出）"
    stderr = state.exec_stderr or ""
    retries = state.retry_times

    lines = [
        "【执行结果】",
        SEPARATOR,
    ]
    if stdout:
        lines.append(f"程序输出：\n{stdout}")
    if stderr:
        lines.append(f"报错信息：\n{stderr}")
    lines += [
        "",
        f"重试次数：{retries} / {state.max_retry}",
        f"任务状态：{'✅ 完成' if not stderr else '⚠️ 有未修复错误'}",
        "",
        SEPARATOR,
        "【最终代码】",
        SEPARATOR,
        state.code,
    ]
    return "\n".join(lines)


def run_agent(requirement: str):
    """主执行入口 — 逐步骤产生中间结果用于 Streaming 显示

    不使用 gr.Progress()（其 DOM overlay 会遮盖输出文字），
    改用 yield 消息头部内嵌进度指示。
    """
    try:
        if not requirement or not requirement.strip():
            yield "请输入有效的开发需求。"
            return

        logger.info(f"Web 收到需求: {requirement}")
        state = CodeAgentState(user_requirement=requirement)

        steps_total = 3 + state.max_retry * 2  # plan + code + (exec+fix)×N
        step_index = 0

        # ── Step 1: Planner ──
        step_index += 1
        _apply_updates(state, planner_node(state))
        plan_text = state.dev_plan[:600] + ("..." if len(state.dev_plan) > 600 else "")
        yield (
            f"> **进度** `[{step_index}/{steps_total}]` 📋 规划中...\n\n"
            f"## 📋 开发方案\n```\n{plan_text}\n```\n\n---\n"
        )

        # ── Step 2: Coder ──
        step_index += 1
        _apply_updates(state, coder_node(state))
        code_snippet = state.code[:800] + ("..." if len(state.code) > 800 else "")
        yield (
            f"> **进度** `[{step_index}/{steps_total}]` ✏️ 编码中...\n\n"
            f"## ✏️ 生成代码（首版）\n```python\n{code_snippet}\n```\n\n---\n"
        )

        # ── Step 3-5: Executor → Judge → Fixer 循环 ──
        iteration = 0
        while iteration <= state.max_retry:
            # Executor
            step_index += 1
            _apply_updates(state, executor_node(state))

            has_error = bool(state.exec_stderr.strip())

            if has_error:
                msg = (
                    f"> **进度** `[{step_index}/{steps_total}]` ▶️ 第 {iteration + 1} 次执行...\n\n"
                    f"## ▶️ 第 {iteration + 1} 次执行结果\n"
                    f"**状态：❌ 报错**\n"
                    f"**输出：**\n```\n{state.exec_stdout[:500]}\n```\n"
                    f"**错误：**\n```\n{state.exec_stderr[:500]}\n```\n\n---\n"
                )
            else:
                msg = (
                    f"> **进度** `[{step_index}/{steps_total}]` ✅ 第 {iteration + 1} 次执行成功\n\n"
                    f"## ▶️ 第 {iteration + 1} 次执行结果\n"
                    f"**状态：✅ 执行成功**\n"
                    f"**输出：**\n```\n{state.exec_stdout[:500]}\n```\n\n---\n"
                )
            yield msg

            # Judge — 决定分支
            judge_route(state)

            if not has_error:
                break

            if state.retry_times >= state.max_retry:
                break

            # Fixer
            iteration += 1
            step_index += 1
            _apply_updates(state, fixer_node(state))
            fix_snippet = state.code[:500] + ("..." if len(state.code) > 500 else "")
            yield (
                f"> **进度** `[{step_index}/{steps_total}]` 🔧 第 {iteration} 次修复中...\n\n"
                f"## 🔧 第 {iteration} 次修复\n"
                f"修复后代码：\n```python\n{fix_snippet}\n```\n\n---\n"
            )

        # ── 结束 ──
        save_path = ""
        if state.code:
            save_path = save_code_to_file(state.code, phase="final")

        final = f"> **进度** `[✓ 完成]` 🎉 任务结束\n\n---\n"
        final += format_output(state)
        if save_path:
            final += f"\n📁 代码已保存至：{save_path}"
        logger.info(f"Web 任务完成，代码保存至: {save_path}")
        yield final

    except Exception as e:
        logger.exception("Web 执行异常")
        yield (
            f"## ❌ 执行出错\n\n"
            f"**错误类型：** `{type(e).__name__}`\n\n"
            f"**错误信息：**\n```\n{e}\n```\n\n"
            f"请查看终端日志（`logs/autocode-agent.log`）获取详细堆栈。"
        )


def list_generated_files() -> str:
    """列出所有已生成的文件"""
    files = get_all_generated_files()
    if not files:
        return "暂无生成文件"
    return "\n".join(f"{i+1}. {f}" for i, f in enumerate(files))


# ── 构建 Gradio 界面 ──
CUSTOM_CSS = """
/* 输出容器 — 最大高度+滚动，防止进度条 overlay 遮住内容 */
.output-box {
    min-height: 420px;
    max-height: 640px;
    overflow-y: auto !important;
    position: relative;
    z-index: 2;
    padding: 8px 4px;
    border-radius: 8px;
    border: 1px solid #e5e7eb;
    background: #fafafa;
}
/* 隐藏 Gradio 内置进度条 overlay（避免遮盖输出文字） */
.progress-bar,
.progress-text,
.gr-progress,
.wrap .svelte-[class*=progress],
.progress-container {
    display: none !important;
    position: absolute !important;
    width: 0 !important;
    height: 0 !important;
    opacity: 0 !important;
    pointer-events: none !important;
    z-index: -1 !important;
}
footer { display: none !important; }
"""

with gr.Blocks(
    title="AutoCodeAgent — 自动编码调试智能体",
    theme=gr.themes.Soft(),
    css=CUSTOM_CSS,
) as demo:
    gr.Markdown(
        """
        # AutoCodeAgent — 自动编码调试智能体

        输入自然语言开发需求，Agent 自动完成 **需求分析 → 代码生成 → 子进程运行 → 报错修复** 全流程。
        """
    )

    with gr.Row():
        with gr.Column(scale=3):
            req_input = gr.Textbox(
                label="💡 输入开发需求",
                lines=4,
                placeholder="例如：实现一个学生成绩管理系统，支持添加学生、查询成绩、计算平均分、删除学生数据",
            )
            with gr.Row():
                submit_btn = gr.Button("🚀 自动生成并调试代码", variant="primary", size="lg")
                clear_btn = gr.Button("🗑️ 清空", size="lg")

        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ 配置")
            gr.Markdown("""- 模型：DeepSeek-V4-Pro\n- 最大重试：5 次\n- 执行超时：15 秒\n- 执行环境：独立子进程""")

    output_box = gr.Markdown(
        value="等待输入需求...",
        label="运行过程与结果",
        elem_classes="output-box",
    )

    with gr.Accordion("📂 已生成的文件", open=False):
        file_list = gr.Textbox(
            value=list_generated_files,
            label="生成文件列表",
            lines=6,
            interactive=False,
        )
        refresh_btn = gr.Button("🔄 刷新文件列表")

    # ── 事件绑定 ──
    submit_btn.click(
        fn=run_agent,
        inputs=[req_input],
        outputs=[output_box],
        concurrency_limit=1,
    )

    clear_btn.click(
        fn=lambda: ("", "等待输入需求..."),
        inputs=[],
        outputs=[req_input, output_box],
    )

    refresh_btn.click(
        fn=list_generated_files,
        inputs=[],
        outputs=[file_list],
    )


if __name__ == "__main__":
    print("=" * 50)
    print("  AutoCodeAgent — 自动编码调试智能体")
    print("  Web 界面启动中...")
    print("=" * 50)
    demo.launch(inbrowser=True, server_port=7870, share=False)
