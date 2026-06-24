"""LangGraph 流程图组装 — StateGraph 闭环调度"""

from langgraph.graph import END, StateGraph

from graph_nodes import coder_node, executor_node, fixer_node, judge_route, planner_node
from state_model import CodeAgentState


def build_code_agent_graph() -> StateGraph:
    """构建完整的 Agent 状态图

    流转链路：
        planner → coder → executor → judge_route
                                        ├── (无报错) → END
                                        └── (有报错) → fixer → executor (循环)
    """
    graph = StateGraph(CodeAgentState)

    # ── 注册节点 ──
    graph.add_node("planner", planner_node)
    graph.add_node("coder", coder_node)
    graph.add_node("executor", executor_node)
    graph.add_node("fixer", fixer_node)

    # ── 固定流转边 ──
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", "executor")

    # ── 条件分支：执行完根据 Judge 决策 ──
    graph.add_conditional_edges(
        "executor",
        judge_route,
        {
            "fixer": "fixer",
            "end_task": END,
        },
    )

    # ── 修复 → 重新执行 ──
    graph.add_edge("fixer", "executor")

    # ── 入口 ──
    graph.set_entry_point("planner")

    return graph.compile()


def create_code_agent():
    """快捷工厂 — 编译好的可调用图"""
    return build_code_agent_graph()
