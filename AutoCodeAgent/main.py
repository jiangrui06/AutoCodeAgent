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
from state_model import CodeAgentState


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
    init_state = CodeAgentState(user_requirement=user_req)

    print(f"\n{'=' * 60}")
    print(f"  AutoCodeAgent — 自动编码调试智能体")
    print(f"{'=' * 60}")
    print(f"  需求：{user_req}")
    print(f"  最大重试：{init_state.max_retry} 次 / 执行超时：15 秒")
    print(f"{'=' * 60}\n")

    logger.info(f"开始执行任务: {user_req}")

    # 执行（流式输出中间状态）
    start_time = time.time()

    final_state = graph.invoke(init_state)

    elapsed = time.time() - start_time
    logger.info(f"任务完成，耗时 {elapsed:.1f}s，重试 {final_state.get('retry_times', 0)} 次")

    # ── 结果输出 ──
    print_separator("任务完成")

    retries = final_state.get("retry_times", 0)
    code = final_state.get("code", "")
    stdout = final_state.get("exec_stdout", "")
    stderr = final_state.get("exec_stderr", "")

    # 持久化最终代码
    if code:
        save_path = save_code_to_file(code, phase="final")
    else:
        save_path = "（无代码生成）"

    print(f"  耗时：{elapsed:.1f} 秒")
    print(f"  重试次数：{retries} / 5")
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
