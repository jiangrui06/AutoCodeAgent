"""Web 界面与核心交互的回归测试。"""

from __future__ import annotations

import subprocess
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import app_web
from dependency_manager import (
    InstallResult,
    MissingDependency,
    code_fingerprint,
    create_execution_permission_context,
    create_install_context,
)
from request_router import RouteDecision


class WebUiStructureTests(unittest.TestCase):
    def test_build_demo_contains_primary_workbench_controls(self) -> None:
        self.assertTrue(callable(app_web.build_demo))
        config = app_web.demo.get_config_file()
        components = config["components"]

        labels = {
            component.get("props", {}).get("label")
            for component in components
        }
        values = {
            component.get("props", {}).get("value")
            for component in components
            if isinstance(component.get("props", {}).get("value"), str)
        }

        self.assertIn("告诉 AutoCodeAgent 你想做什么", labels)
        self.assertIn("权限等级", labels)
        self.assertIn("上传图片或文件", labels)
        self.assertIn("发送", values)
        self.assertIn("新对话", values)
        self.assertIn("刷新文件", values)
        self.assertIn("允许本次操作", values)
        self.assertIn("拒绝并停止", values)

    def test_permission_buttons_map_pending_context_to_explicit_answers(self) -> None:
        execution_pending = create_execution_permission_context(
            "调用外部工具",
            "import subprocess\nsubprocess.run(['tool'])\n",
            "执行工具",
        )
        install_pending = create_install_context(
            "写一个 PyQt5 界面",
            MissingDependency("PyQt5", "PyQt5"),
        )

        self.assertEqual(
            app_web._pending_permission_response(execution_pending, approved=True),
            "允许执行",
        )
        self.assertEqual(
            app_web._pending_permission_response(execution_pending, approved=False),
            "拒绝执行",
        )
        self.assertEqual(
            app_web._pending_permission_response(install_pending, approved=True),
            "允许安装",
        )
        self.assertEqual(
            app_web._pending_permission_response(install_pending, approved=False),
            "取消安装",
        )
        self.assertEqual(app_web._pending_permission_response("", approved=True), "")

    def test_css_includes_tokens_responsive_and_accessibility_rules(self) -> None:
        css = app_web.CUSTOM_CSS

        self.assertIn("--color-bg", css)
        self.assertIn("--color-accent", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn(".permission-card > .styler", css)
        self.assertIn(".composer-surface > .styler", css)
        self.assertNotIn("#6366f1", css.lower())

    def test_reset_conversation_clears_all_session_state(self) -> None:
        self.assertEqual(
            app_web.reset_conversation(),
            ("", app_web.EMPTY_OUTPUT, "", "", "", ""),
        )

    @patch("app_web.subprocess.run")
    def test_run_saved_gui_code_uses_utf8_self_test_mode(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GUI self-test passed\n", stderr=""
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "gui_app.py"
            path.write_text(
                'print("✓")\n# --autocode-self-test\n', encoding="utf-8"
            )

            result = app_web.run_saved_code(str(path))

        command = run.call_args.args[0]
        self.assertEqual(command[:4], [app_web.sys.executable, "-I", "-X", "utf8"])
        self.assertEqual(command[-1], "--autocode-self-test")
        self.assertIn("运行成功", result)

    def test_build_demo_hides_only_known_gradio6_migration_warnings(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app_web.build_demo()

        messages = [str(item.message) for item in caught]
        self.assertFalse(any("'theme' parameter" in item for item in messages))
        self.assertFalse(any("'css' parameter" in item for item in messages))

    @patch("app_web._is_autocodeagent_server", return_value=True)
    @patch("app_web._port_is_available", return_value=False)
    def test_launch_target_reuses_an_existing_autocodeagent(
        self, _available, _existing
    ) -> None:
        port, reuse = app_web._resolve_web_launch("127.0.0.1", 7870)

        self.assertEqual(port, 7870)
        self.assertTrue(reuse)

    @patch("app_web._is_autocodeagent_server", return_value=False)
    @patch("app_web._port_is_available", side_effect=[False, True])
    def test_launch_target_falls_back_when_another_service_owns_the_port(
        self, _available, _existing
    ) -> None:
        port, reuse = app_web._resolve_web_launch("127.0.0.1", 7870)

        self.assertEqual(port, 7871)
        self.assertFalse(reuse)

    def test_launch_limits_each_uploaded_file_size(self) -> None:
        options = app_web._launch_options(7870)

        self.assertEqual(options["max_file_size"], "10mb")

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch(
        "app_web.route_user_request",
        return_value=RouteDecision("chat", "已收到附件。"),
    )
    @patch("app_web.build_attachment_context", return_value="ATTACHMENT-CONTEXT")
    @patch("app_web.prepare_attachments", return_value=(MagicMock(),))
    def test_uploaded_files_are_added_to_request_context(
        self,
        prepare,
        _build_context,
        route,
        _memory,
        _validate,
    ) -> None:
        result = list(app_web.run_agent("检查这个文件", attachments=["sample.png"]))

        prepare.assert_called_once()
        routed_requirement = route.call_args.args[0]
        self.assertIn("检查这个文件", routed_requirement)
        self.assertIn("ATTACHMENT-CONTEXT", routed_requirement)
        self.assertIn("已收到附件", result[-1][0])


class WebAgentFlowTests(unittest.TestCase):
    def test_trusted_mode_reuses_only_an_equal_or_smaller_approved_scope(self) -> None:
        state = MagicMock(
            approved_code_hash="approved-once",
            approved_capabilities=("filesystem_delete", "filesystem_write"),
            approved_security_findings=("文件写入操作", "文件删除操作"),
        )
        same_report = MagicMock(
            missing_dependencies=(),
            capabilities=(
                MagicMock(key="filesystem_write"),
                MagicMock(key="filesystem_delete"),
            ),
        )
        same_security = MagicMock(
            findings=(
                MagicMock(title="文件写入操作"),
                MagicMock(title="文件删除操作"),
            )
        )
        escalated_report = MagicMock(
            missing_dependencies=(),
            capabilities=(MagicMock(key="system_command"),),
        )

        self.assertTrue(
            app_web._trusted_approval_covers(state, same_report, same_security)
        )
        self.assertFalse(
            app_web._trusted_approval_covers(state, escalated_report, same_security)
        )

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch(
        "app_web.route_user_request",
        return_value=RouteDecision("chat", "你好，我记得你喜欢在 Obsidian 查看日志。"),
    )
    def test_chat_request_returns_answer_without_coding(
        self,
        _route,
        _memory,
        _validate,
    ) -> None:
        result = list(app_web.run_agent("你好"))

        self.assertEqual(len(result), 1)
        self.assertIn("Obsidian", result[0][0])
        self.assertEqual(result[0][1], "")

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch(
        "app_web.route_user_request",
        return_value=RouteDecision("clarify", "你希望它运行在网页还是桌面？"),
    )
    def test_clarification_preserves_pending_requirement(
        self,
        _route,
        _memory,
        _validate,
    ) -> None:
        result = list(app_web.run_agent("帮我做一个工具"))

        self.assertEqual(len(result), 1)
        self.assertIn("需要你确认", result[0][0])
        self.assertIn("帮我做一个工具", result[0][1])
        self.assertIn("你希望它运行在网页还是桌面？", result[0][1])

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch("app_web.route_user_request", return_value=RouteDecision("code"))
    @patch("app_web.planner_node", return_value={"dev_plan": "先输出问候"})
    @patch("app_web.coder_node", return_value={"code": "print('hello')"})
    @patch(
        "app_web.executor_node",
        return_value={"exec_stdout": "hello\n", "exec_stderr": ""},
    )
    @patch("app_web.save_code_to_file", return_value="generated.py")
    def test_code_request_streams_plan_execution_and_final_result(
        self,
        _save,
        _execute,
        _code,
        _plan,
        _route,
        _memory,
        _validate,
    ) -> None:
        result = list(app_web.run_agent("写一个打印 hello 的程序"))
        combined = "\n".join(item[0] for item in result)

        self.assertGreaterEqual(len(result), 4)
        self.assertIn("开发方案", combined)
        self.assertIn("执行成功", combined)
        self.assertIn("任务完成", combined)
        self.assertIn("generated.py", combined)

    def test_generated_file_list_handles_empty_and_populated_results(self) -> None:
        with patch("app_web.get_all_generated_files", return_value=[]):
            self.assertEqual(app_web.list_generated_files(), "暂时还没有生成文件")

        with TemporaryDirectory() as temp_dir:
            files = [Path(temp_dir) / "alpha.py", Path(temp_dir) / "beta.py"]
            with patch("app_web.get_all_generated_files", return_value=files):
                result = app_web.list_generated_files()

        self.assertIn("alpha.py", result)
        self.assertIn("beta.py", result)

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch("app_web.route_user_request", return_value=RouteDecision("code"))
    @patch("app_web.planner_node", return_value={"dev_plan": "使用 PyQt5 创建登录窗口"})
    @patch("app_web.coder_node", return_value={"code": "from PyQt5.QtWidgets import QApplication"})
    @patch(
        "app_web.executor_node",
        return_value={
            "exec_stdout": "",
            "exec_stderr": "ModuleNotFoundError: No module named 'PyQt5'",
        },
    )
    @patch("app_web.fixer_node")
    def test_missing_dependency_pauses_before_fixer_and_asks_permission(
        self,
        fixer,
        _execute,
        _code,
        _plan,
        _route,
        _memory,
        _validate,
    ) -> None:
        result = list(app_web.run_agent("写一个 PyQt 登录界面"))
        (
            final_message,
            pending_context,
            _session_id,
            _copy_value,
            _code_value,
            _saved_path,
        ) = result[-1]

        self.assertIn("需要安装依赖", final_message)
        self.assertIn("PyQt5", final_message)
        self.assertTrue(pending_context.startswith(app_web.INSTALL_CONTEXT_PREFIX))
        fixer.assert_not_called()

    @patch("config.Settings.validate_llm_config")
    @patch("app_web.get_memory_store", return_value=None)
    @patch("app_web.route_user_request", return_value=RouteDecision("code"))
    @patch("app_web.planner_node", return_value={"dev_plan": "使用 PyQt5 创建登录窗口"})
    @patch(
        "app_web.coder_node",
        return_value={
            "code": (
                "try:\n"
                "    from PyQt5.QtWidgets import QApplication\n"
                "except ImportError:\n"
                "    print('PyQt5 未安装，将使用文本界面模式')\n"
            )
        },
    )
    @patch(
        "app_web.executor_node",
        return_value={"exec_stdout": "文本界面模式\n", "exec_stderr": ""},
    )
    @patch("app_web.save_code_to_file", return_value="unused.py")
    def test_import_fallback_cannot_hide_missing_dependency_permission(
        self,
        _save,
        executor,
        _code,
        _plan,
        _route,
        _memory,
        _validate,
    ) -> None:
        with patch("dependency_manager.importlib.util.find_spec", return_value=None):
            result = list(app_web.run_agent("写一个 PyQt5 登录界面"))

        final_message, pending_context, *_rest = result[-1]
        self.assertIn("执行前权限确认", final_message)
        self.assertIn("PyQt5", final_message)
        self.assertTrue(pending_context.startswith("execution-permission:"))
        executor.assert_not_called()

    def test_ask_mode_prompts_for_cv2_install_instead_of_blocking(self) -> None:
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "使用 OpenCV"}),
            patch("app_web.coder_node", return_value={"code": "import cv2\n"}),
            patch("app_web.executor_node") as executor,
            patch("dependency_manager.importlib.util.find_spec", return_value=None),
        ):
            result = list(
                app_web.run_agent("使用 OpenCV 处理图片", permission_level="ask")
            )

        final_message, pending_context, *_rest = result[-1]
        self.assertIn("执行前权限确认", final_message)
        self.assertIn("cv2", final_message)
        self.assertIn("opencv-python", final_message)
        self.assertTrue(pending_context.startswith("execution-permission:"))
        executor.assert_not_called()

    def test_ask_mode_unknown_dependency_does_not_suggest_switching_to_ask(self) -> None:
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "使用未知模块"}),
            patch("app_web.coder_node", return_value={"code": "import mystery_dep\n"}),
            patch("app_web.executor_node") as executor,
            patch("dependency_manager.importlib.util.find_spec", return_value=None),
        ):
            result = list(
                app_web.run_agent("使用 mystery_dep", permission_level="ask")
            )

        final_message, pending_context, *_rest = result[-1]
        self.assertIn("权限策略已阻止执行", final_message)
        self.assertIn("无法安全确定", final_message)
        self.assertNotIn("切换到“询问模式”", final_message)
        self.assertEqual(pending_context, "")
        executor.assert_not_called()

    def test_execution_approval_resumes_the_exact_generated_code(self) -> None:
        code = "import subprocess\nsubprocess.run(['tool', '--version'])\n"
        pending = create_execution_permission_context(
            "调用一个外部工具",
            code,
            "调用工具并展示版本",
        )
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch(
                "app_web.executor_node",
                return_value={"exec_stdout": "tool 1.0\n", "exec_stderr": ""},
            ) as execute,
            patch("app_web.save_code_to_file", return_value="tool.py"),
        ):
            result = list(app_web.approve_pending_permission(pending, "", "ask"))

        resumed_state = execute.call_args.args[0]
        combined = "\n".join(item[0] for item in result)
        self.assertEqual(resumed_state.code, code)
        self.assertEqual(resumed_state.approved_code_hash, code_fingerprint(code))
        self.assertIn("本次权限已确认", combined)
        self.assertIn("任务完成", combined)

    def test_trusted_approval_continues_repairs_without_reasking_same_scope(self) -> None:
        original_code = (
            "from pathlib import Path\n"
            "Path('first.txt').write_text('first', encoding='utf-8')\n"
        )
        fixed_code = (
            "from pathlib import Path\n"
            "Path('second.txt').write_text('second', encoding='utf-8')\n"
        )
        pending = create_execution_permission_context(
            "写入一个本地测试文件",
            original_code,
            "写入文件并验证",
        )
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch(
                "app_web.executor_node",
                side_effect=[
                    {"exec_stdout": "", "exec_stderr": "AssertionError", "exec_exit_code": 1},
                    {"exec_stdout": "fixed\n", "exec_stderr": "", "exec_exit_code": 0},
                ],
            ) as execute,
            patch(
                "app_web.diagnose_failure_node",
                return_value={"failure_analysis": "写入内容错误。"},
            ),
            patch(
                "app_web.fixer_node",
                return_value={
                    "code": fixed_code,
                    "retry_times": 1,
                    "exec_stdout": "",
                    "exec_stderr": "",
                    "fix_history": [],
                    "no_progress": False,
                },
            ),
            patch("app_web.save_code_to_file", return_value="fixed_file.py"),
        ):
            result = list(
                app_web.approve_pending_permission(pending, "", "trusted")
            )

        combined = "\n".join(item[0] for item in result)
        second_state = execute.call_args_list[1].args[0]
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(second_state.approved_code_hash, code_fingerprint(fixed_code))
        self.assertNotIn("执行前权限确认", combined)
        self.assertIn("任务完成", combined)

    def test_trusted_level_auto_installs_only_allowlisted_dependency(self) -> None:
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "使用 PyQt5"}),
            patch(
                "app_web.coder_node",
                return_value={"code": "from PyQt5.QtWidgets import QApplication\n"},
            ),
            patch(
                "app_web.executor_node",
                return_value={"exec_stdout": "GUI ready\n", "exec_stderr": ""},
            ),
            patch("app_web.install_dependency", return_value=InstallResult(True, "PyQt5", "安装成功")) as install,
            patch("app_web.save_code_to_file", return_value="trusted.py"),
            patch("dependency_manager.importlib.util.find_spec", return_value=None),
        ):
            result = list(app_web.run_agent("写一个 PyQt5 界面", permission_level="trusted"))

        combined = "\n".join(item[0] for item in result)
        install.assert_called_once_with("PyQt5")
        self.assertIn("自动安装白名单依赖 PyQt5", combined)
        self.assertNotIn("执行前权限确认", combined)
        self.assertIn("任务完成", combined)

    def test_approved_dependency_install_resumes_original_code_requirement(self) -> None:
        pending = create_install_context(
            "写一个 PyQt 登录界面",
            MissingDependency("PyQt5", "PyQt5"),
        )
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch("app_web.install_dependency", return_value=InstallResult(True, "PyQt5", "依赖 PyQt5 安装成功。")) as install,
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "使用 PyQt5"}),
            patch("app_web.coder_node", return_value={"code": "print('gui ready')"}),
            patch(
                "app_web.executor_node",
                return_value={"exec_stdout": "gui ready\n", "exec_stderr": ""},
            ),
            patch("app_web.save_code_to_file", return_value="pyqt_login.py"),
        ):
            result = list(app_web.run_agent("允许安装", pending))

        combined = "\n".join(item[0] for item in result)
        install.assert_called_once_with("PyQt5")
        self.assertIn("正在安装 PyQt5", combined)
        self.assertIn("任务完成", combined)

    def test_unchanged_fixer_output_stops_without_repeating_all_retries(self) -> None:
        unchanged_code = "print(missing_name)\n"
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=None),
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "输出变量"}),
            patch("app_web.coder_node", return_value={"code": unchanged_code}),
            patch(
                "app_web.executor_node",
                return_value={"exec_stdout": "", "exec_stderr": "NameError: missing_name"},
            ) as execute,
            patch(
                "app_web.diagnose_failure_node",
                return_value={"failure_analysis": "missing_name 在使用前没有定义。"},
            ),
            patch(
                "app_web.fixer_node",
                return_value={
                    "code": unchanged_code,
                    "retry_times": 1,
                    "exec_stdout": "",
                    "exec_stderr": "[NoProgress] 修复器返回了完全相同的代码。",
                    "fix_history": [],
                    "no_progress": True,
                },
            ) as fixer,
            patch("app_web.save_code_to_file", return_value="unchanged.py"),
        ):
            result = list(app_web.run_agent("修复这个变量错误"))

        combined = "\n".join(item[0] for item in result)
        self.assertEqual(execute.call_count, 1)
        fixer.assert_called_once()
        self.assertIn("正在分析第 1 次失败", combined)
        self.assertIn("根因分析完成，正在生成修复", combined)
        self.assertIn("停止重复修复", combined)

    def test_error_memory_is_read_before_attempts_and_resolved_after_success(self) -> None:
        memory = MagicMock()
        memory.create_session.return_value = "session-1"
        memory.recall.return_value = ""
        memory.recall_error_experiences.return_value = (
            "[已验证经验 abc123] 使用 pyqtSignal，不要导入 Signal"
        )
        memory.record_error.return_value = "error-signature"
        with (
            patch("config.Settings.validate_llm_config"),
            patch("app_web.get_memory_store", return_value=memory),
            patch("app_web.route_user_request", return_value=RouteDecision("code")),
            patch("app_web.planner_node", return_value={"dev_plan": "PyQt 窗口"}),
            patch(
                "app_web.coder_node",
                return_value={"code": "from PyQt6.QtCore import Signal\n"},
            ),
            patch(
                "app_web.executor_node",
                side_effect=[
                    {
                        "exec_stdout": "SELF_TEST_FAILED: signal import\n",
                        "exec_stderr": "ImportError: Signal",
                        "exec_exit_code": 1,
                    },
                    {
                        "exec_stdout": "self-test passed\n",
                        "exec_stderr": "",
                        "exec_exit_code": 0,
                    },
                ],
            ),
            patch(
                "app_web.diagnose_failure_node",
                return_value={
                    "failure_analysis": "PyQt6 应导入 pyqtSignal，而不是 Signal。"
                },
            ),
            patch(
                "app_web.fixer_node",
                return_value={
                    "code": "from PyQt6.QtCore import pyqtSignal\n",
                    "retry_times": 1,
                    "exec_stdout": "",
                    "exec_stderr": "",
                    "fix_history": [],
                    "no_progress": False,
                },
            ),
            patch("app_web.save_code_to_file", return_value="fixed.py"),
        ):
            list(app_web.run_agent("写一个 PyQt 登录界面"))

        self.assertGreaterEqual(memory.recall_error_experiences.call_count, 2)
        memory.record_error.assert_called_once()
        recorded_failure = memory.record_error.call_args.args[3]
        self.assertIn("SELF_TEST_FAILED", recorded_failure)
        stdout_entries = [
            call
            for call in memory.add_entry.call_args_list
            if len(call.args) >= 4 and call.args[3] == "stdout"
        ]
        self.assertEqual(stdout_entries[0].args[4]["exit_code"], 1)
        self.assertIn("code_hash", stdout_entries[0].args[4])
        memory.resolve_errors.assert_called_once_with(
            ("error-signature",),
            "from PyQt6.QtCore import pyqtSignal\n",
        )


if __name__ == "__main__":
    unittest.main()
