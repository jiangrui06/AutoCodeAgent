"""AutoCodeAgent 命令行入口

用法：
    python main.py
    python main.py "写一个计算器程序"
"""

import sys
import time

from file_util import save_code_to_file
from graph_builder import build_code_agent_graph
from logger import logger
from memory_store import get_memory_store
from request_router import route_user_request
from state_model import CodeAgentState
from config import settings
from dependency_manager import (
    detect_missing_dependency,
    install_dependency,
    is_install_approved,
)


def print_separator(title: str = ""):
    """打印分隔线"""
    width = 60
    if title:
        title = f" {title} "
        side = "=" * ((width - len(title)) // 2)
        print(f"\n{side}{title}{side}")
    else:
        print("=" * width)


def run_auto_code_agent(user_req: str) -> dict:
    """运行完整 Agent 流程

    Args:
        user_req: 用户自然语言开发需求

    Returns:
        最终状态字典
    """
    # 编译图
    graph = build_code_agent_graph()

    # 初始化状态
    settings.validate_llm_config()
    memory = get_memory_store()
    session_id = memory.create_session(user_req) if memory else ""
    if memory:
        memory.add_entry(session_id, "user", user_req)
    memory_context = memory.recall(session_id) if memory else ""
    decision = route_user_request(user_req, memory_context)
    if memory:
        memory.remember(decision.memories, session_id)
    if decision.mode == "chat":
        if memory:
            memory.add_entry(session_id, "assistant", decision.message)
        print(f"\nAutoCodeAgent：{decision.message}")
        return {"mode": "chat", "message": decision.message}
    if decision.mode == "clarify":
        if memory:
            memory.add_entry(session_id, "assistant", decision.message, "clarify")
        print(f"\n需要确认：{decision.message}")
        return {"mode": "clarify", "message": decision.message}

    agent_requirement = user_req
    if memory_context and any(word in user_req for word in ("之前", "上次", "继续", "记得", "我们")):
        agent_requirement += f"\n\n以下是可参考的长期记忆：\n{memory_context}"
    init_state = CodeAgentState(
        user_requirement=agent_requirement,
        max_retry=settings.agent_max_retry,
    )

    print(f"\n{'=' * 60}")
    print("  AutoCodeAgent — 自动编码调试智能体")
    print(f"{'=' * 60}")
    print(f"  需求：{user_req}")
    print(f"  模型：{settings.llm_model}")
    print(f"  最大重试：{init_state.max_retry} 次 / 执行超时：{settings.sandbox_timeout} 秒")
    print(f"{'=' * 60}\n")

    logger.info(f"开始执行任务: {user_req}")

    # 执行（流式输出中间状态）
    start_time = time.time()

    final_state = graph.invoke(init_state)

    missing_dependency = detect_missing_dependency(final_state.get("exec_stderr", ""))
    if missing_dependency and missing_dependency.package and sys.stdin.isatty():
        print(
            f"\n检测到缺少依赖 {missing_dependency.package}。"
            "是否允许安装到当前虚拟环境？输入“允许安装”确认："
        )
        answer = input("安装许可 > ").strip()
        if is_install_approved(answer):
            if memory:
                memory.add_entry(session_id, "user", answer)
            install_result = install_dependency(missing_dependency.package)
            print(install_result.message)
            if memory:
                memory.add_entry(
                    session_id,
                    "system",
                    install_result.message,
                    "status" if install_result.success else "stderr",
                )
            if install_result.success:
                resumed_requirement = (
                    f"{agent_requirement}\n\n"
                    f"用户已许可安装 {missing_dependency.package}，安装已经成功。"
                    "必须继续使用原需求指定的框架，不得替换成其他库或命令行实现。"
                )
                init_state = CodeAgentState(
                    user_requirement=resumed_requirement,
                    max_retry=settings.agent_max_retry,
                )
                final_state = graph.invoke(init_state)
        else:
            print("已取消安装，本次任务停止。")

    elapsed = time.time() - start_time
    logger.info(f"任务完成，耗时 {elapsed:.1f}s，重试 {final_state.get('retry_times', 0)} 次")

    # ── 结果输出 ──
    print_separator("任务完成")

    retries = final_state.get("retry_times", 0)
    code = final_state.get("code", "")
    stdout = final_state.get("exec_stdout", "")
    stderr = final_state.get("exec_stderr", "")

    if memory:
        memory.add_entry(session_id, "assistant", final_state.get("dev_plan", ""), "plan")
        memory.add_entry(session_id, "assistant", code, "code")
        memory.add_entry(session_id, "system", stdout, "stdout")
        memory.add_entry(session_id, "system", stderr, "stderr")
        memory.add_entry(
            session_id,
            "system",
            "任务完成" if not stderr else "任务结束但仍有错误",
            "status",
            {"retry_times": retries},
        )

    # 持久化最终代码
    if code:
        save_path = save_code_to_file(code, phase="final")
    else:
        save_path = "（无代码生成）"

    print(f"  耗时：{elapsed:.1f} 秒")
    print(f"  重试次数：{retries} / {init_state.max_retry}")
    print(f"  代码保存：{save_path}")

    if stdout:
        print_separator("程序输出")
        print(stdout[:2000])  # 限制输出长度

    if stderr:
        print_separator("最终报错")
        print(stderr[:2000])
        logger.warning(f"执行存在报错: {stderr[:500]}")

    print_separator("最终代码")
    print(code)

    logger.info(f"最终代码已保存: {save_path}")

    return final_state


def main():
    # 从命令行参数或交互式输入获取需求
    if len(sys.argv) > 1:
        requirement = " ".join(sys.argv[1:])
    else:
        print("AutoCodeAgent — 请输入开发需求（输入空行退出）：")
        requirement = input("需求 > ").strip()
        if not requirement:
            print("已退出。")
            return

    run_auto_code_agent(requirement)


if __name__ == "__main__":
    main()
