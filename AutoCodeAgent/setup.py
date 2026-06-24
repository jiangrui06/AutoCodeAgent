"""AutoCodeAgent — 自动编码调试智能体

输入自然语言需求，全自动完成代码生成、执行、报错修复的闭环流程。
"""

from setuptools import find_packages, setup

with open("requirements.txt", encoding="utf-8") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="autocode-agent",
    version="1.0.0",
    description="基于 LangGraph 的自动编码调试智能体 — 输入需求，自动生成、执行、修复代码",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="AutoCodeAgent Team",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "autocode=main:main",
        ],
    },
)
