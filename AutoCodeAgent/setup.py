"""AutoCodeAgent — 自动编码调试智能体

输入自然语言需求，全自动完成代码生成、执行、报错修复的闭环流程。
"""

from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent

with (ROOT / "requirements.txt").open(encoding="utf-8") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="autocode-agent",
    version="1.0.0",
    description="基于 LangGraph 的自动编码调试智能体 — 输入需求，自动生成、执行、修复代码",
    long_description=(ROOT.parent / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="AutoCodeAgent Team",
    py_modules=[
        "app_web",
        "attachment_manager",
        "code_linter",
        "code_sandbox",
        "code_scanner",
        "config",
        "dependency_manager",
        "file_util",
        "graph_builder",
        "graph_nodes",
        "llm_client",
        "logger",
        "main",
        "memory_store",
        "openhands_adapter",
        "openhands_worker",
        "openhands_workspace_security",
        "request_router",
        "state_model",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "autocode=main:main",
        ],
    },
)
