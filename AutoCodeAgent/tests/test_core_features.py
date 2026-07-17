"""AutoCodeAgent 核心模块的本地集成测试。"""

from __future__ import annotations

import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dependency_manager
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from code_linter import lint_code
from code_sandbox import safe_execute_code
from code_scanner import CRITICAL, scan_code
from config import Settings
from dependency_manager import (
    MissingDependency,
    detect_code_dependencies,
    detect_missing_dependency,
    install_dependency,
    is_install_approved,
)
from file_util import get_all_generated_files, get_latest_code_file, save_code_to_file
from graph_builder import create_code_agent
from graph_nodes import (
    CODER_PROMPT,
    DIAGNOSER_PROMPT,
    FIXER_PROMPT,
    _build_failure_focus_context,
    coder_node,
    executor_node,
    fixer_node,
    judge_route,
    validate_gui_interaction_contract,
)
from memory_store import MemoryStore
from request_router import _fallback_route
from state_model import CodeAgentState


class ConfigurationTests(unittest.TestCase):
    def test_valid_llm_configuration_is_accepted(self) -> None:
        config = Settings(
            llm_api_key="sk-test-key-1234567890",
            llm_base_url="https://example.test/v1/",
            llm_model="test-model",
        )

        config.validate_llm_config()
        self.assertTrue(config.is_llm_configured)
        self.assertEqual(config.base_url, "https://example.test/v1")


class RoutingTests(unittest.TestCase):
    def test_fallback_router_separates_chat_code_and_ambiguous_input(self) -> None:
        self.assertEqual(_fallback_route("你好").mode, "chat")
        self.assertEqual(_fallback_route("帮我写一个 Python 脚本").mode, "code")
        self.assertEqual(_fallback_route("我有一个想法").mode, "clarify")


class CodePipelineTests(unittest.TestCase):
    def test_coder_node_returns_extracted_code(self) -> None:
        state = CodeAgentState(
            user_requirement="写一个程序",
            dev_plan="输出 hello",
        )
        fake_llm = RunnableLambda(
            lambda _prompt: AIMessage(
                content="```python\ndef main():\n    print('hello')\n```"
            )
        )

        with (
            patch("graph_nodes.get_deepseek_llm", return_value=fake_llm),
            patch("graph_nodes.save_iteration_snapshot") as save_snapshot,
        ):
            updates = coder_node(state)

        self.assertIn("def main()", updates["code"])
        save_snapshot.assert_called_once_with(updates["code"], retry=0)

    def test_fixer_uses_fresh_rebuild_when_first_fix_makes_no_progress(self) -> None:
        old_code = "print(missing_name)"
        rebuilt_code = "def main():\n    print('fixed')\n\nif __name__ == '__main__':\n    main()"
        responses = iter(
            [
                AIMessage(content=f"```python\n{old_code}```"),
                AIMessage(content=f"```python\n{rebuilt_code}```"),
            ]
        )
        fake_llm = RunnableLambda(lambda _prompt: next(responses))
        state = CodeAgentState(
            user_requirement="修复未定义变量",
            dev_plan="输出 fixed",
            code=old_code,
            exec_stderr="NameError: missing_name",
            exec_exit_code=1,
            failure_analysis="变量未定义",
        )

        with (
            patch("graph_nodes.get_deepseek_llm", return_value=fake_llm),
            patch("graph_nodes.save_iteration_snapshot") as save_snapshot,
        ):
            updates = fixer_node(state)

        self.assertEqual(updates["code"], rebuilt_code)
        self.assertFalse(updates["no_progress"])
        save_snapshot.assert_called_once_with(rebuilt_code, retry=1)

    def test_linter_accepts_valid_code_and_rejects_undefined_names(self) -> None:
        valid, message = lint_code("value = 2 + 3\nprint(value)\n")
        invalid, error = lint_code("print(missing_value)\n")

        self.assertTrue(valid, message)
        self.assertFalse(invalid)
        self.assertIn("undefined name", error.lower())

    def test_scanner_allows_simple_code_and_flags_command_execution(self) -> None:
        safe_report = scan_code("print(2 + 3)\n")
        dangerous_report = scan_code(
            "import subprocess\nsubprocess.run(['cmd', '/c', 'echo', 'unsafe'])\n"
        )

        self.assertTrue(safe_report.is_safe)
        self.assertGreaterEqual(dangerous_report.max_risk, CRITICAL)

    def test_tool_permission_is_bound_to_the_exact_code_fingerprint(self) -> None:
        code = "import subprocess\nsubprocess.run(['tool', '--version'])\n"
        approved = CodeAgentState(
            user_requirement="调用外部工具",
            code=code,
            approved_code_hash=dependency_manager.code_fingerprint(code),
        )
        changed = CodeAgentState(
            user_requirement="调用外部工具",
            code=code + "print('changed')\n",
            approved_code_hash=dependency_manager.code_fingerprint(code),
        )

        with (
            patch("graph_nodes.sys.stdin.isatty", return_value=False),
            patch("graph_nodes.safe_execute_code", return_value=("ok\n", "")) as execute,
        ):
            approved_result = executor_node(approved)
            changed_result = executor_node(changed)

        self.assertEqual(approved_result["exec_stderr"], "")
        self.assertIn("安全扫描未通过", changed_result["exec_stderr"])
        execute.assert_called_once_with(code)

    def test_sandbox_executes_unicode_and_stops_infinite_loop(self) -> None:
        stdout, stderr = safe_execute_code("print('沙箱可用')\n", timeout=3)
        timeout_stdout, timeout_stderr = safe_execute_code("while True:\n    pass\n", timeout=1)

        self.assertIn("沙箱可用", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(timeout_stdout, "")
        self.assertIn("执行超时", timeout_stderr)

    def test_sandbox_uses_utf8_for_symbols_outside_windows_gbk(self) -> None:
        stdout, stderr = safe_execute_code("print('✓ PyQt 自检通过')\n", timeout=3)

        self.assertEqual(stdout.strip(), "✓ PyQt 自检通过")
        self.assertEqual(stderr, "")

    def test_executor_treats_stderr_warning_as_nonfatal_when_exit_code_is_zero(self) -> None:
        state = CodeAgentState(
            user_requirement="运行会输出 Qt 警告的程序",
            code=(
                "import sys\n"
                "print('QFontDatabase warning', file=sys.stderr)\n"
                "print('self-test passed')\n"
            ),
        )

        updates = executor_node(state)

        self.assertEqual(updates["exec_exit_code"], 0)
        self.assertEqual(updates["exec_stderr"], "")
        self.assertIn("[RuntimeWarning]", updates["exec_stdout"])
        self.assertIn("QFontDatabase warning", updates["exec_stdout"])

    def test_executor_preserves_stdout_as_failure_evidence_for_nonzero_exit(self) -> None:
        state = CodeAgentState(
            user_requirement="运行自检",
            code="print('SELF_TEST_FAILED: login message cleared')\nraise SystemExit(1)\n",
        )

        updates = executor_node(state)

        self.assertEqual(updates["exec_exit_code"], 1)
        self.assertIn("[ExitCode 1]", updates["exec_stderr"])
        self.assertIn("[CapturedStdout]", updates["exec_stderr"])
        self.assertIn("SELF_TEST_FAILED", updates["exec_stderr"])

    def test_failure_focus_context_expands_failed_check_and_called_methods(self) -> None:
        code = '''
class Form:
    def clear_form(self):
        self.info_label.clear()

class Window:
    def handle_login(self):
        self.form.info_label.setText("登录成功")
        self.form.clear_form()

def run_self_test():
    window.handle_login()
    if "登录成功" not in window.form.info_label.text():
        print("✗ 正常登录测试失败")
        return 1
'''

        context = _build_failure_focus_context(code, "✗ 正常登录测试失败")

        self.assertIn("run_self_test", context)
        self.assertIn("handle_login", context)
        self.assertIn("clear_form", context)
        self.assertIn("self.info_label.clear()", context)

    def test_sandbox_uses_declared_gui_self_test_mode(self) -> None:
        code = """
import sys

if "--autocode-self-test" in sys.argv:
    print("GUI self-test passed")
else:
    while True:
        pass
"""

        stdout, stderr = safe_execute_code(code, timeout=2)

        self.assertIn("GUI self-test passed", stdout)
        self.assertEqual(stderr, "")

    def test_gui_contract_rejects_decorative_actions_and_accepts_observable_tests(self) -> None:
        decorative = '''
from PyQt5.QtWidgets import QAction

def run_self_test():
    print("菜单存在")

save_action = QAction("保存")
'''
        functional = '''
from tempfile import TemporaryDirectory
from PyQt5.QtWidgets import QAction

def _test_file_actions():
    with TemporaryDirectory() as temp_dir:
        save_action.trigger()
        assert temp_dir

def run_all_tests():
    _test_file_actions()

save_action = QAction("保存")
'''
        partial_coverage = '''
from tempfile import TemporaryDirectory
from PyQt5.QtWidgets import QAction

def run_self_test():
    with TemporaryDirectory():
        actions['new'].trigger()
        assert changed

actions = {}
actions['new'] = QAction("新建")
actions['save'] = QAction("保存")
changed = True
'''

        decorative_ok, decorative_error = validate_gui_interaction_contract(decorative)
        functional_ok, functional_error = validate_gui_interaction_contract(functional)
        partial_ok, partial_error = validate_gui_interaction_contract(partial_coverage)

        self.assertFalse(decorative_ok)
        self.assertIn("QAction.trigger()", decorative_error)
        self.assertFalse(partial_ok)
        self.assertIn("save", partial_error)
        self.assertTrue(functional_ok, functional_error)

    def test_gui_contract_rejects_nonexistent_qt_move_mode(self) -> None:
        ok, error = validate_gui_interaction_contract(
            "from PyQt6.QtCore import Qt\nmode = Qt.MoveMode.KeepAnchor\n"
        )

        self.assertFalse(ok)
        self.assertIn("QTextCursor.MoveMode.KeepAnchor", error)

    def test_langgraph_contains_the_complete_retry_loop(self) -> None:
        graph = create_code_agent().get_graph()

        self.assertTrue(
            {"planner", "coder", "executor", "diagnoser", "fixer"}.issubset(
                graph.nodes
            )
        )

    def test_missing_dependency_never_enters_fixer(self) -> None:
        state = CodeAgentState(
            user_requirement="写一个 PyQt 登录界面",
            code="from PyQt5.QtWidgets import QApplication",
            exec_stderr="ModuleNotFoundError: No module named 'PyQt5'",
        )

        self.assertEqual(judge_route(state), "end_task")
        self.assertIn("不得删除或替换用户明确指定的框架", FIXER_PROMPT.template)

    def test_pyqt_prompts_require_version_correct_scoped_enums(self) -> None:
        for prompt in (CODER_PROMPT.template, FIXER_PROMPT.template):
            self.assertIn("QFont.Weight.Bold", prompt)
            self.assertIn("Qt.AlignmentFlag.AlignCenter", prompt)
            self.assertIn("QLineEdit.EchoMode.Password", prompt)
            self.assertIn("可见交互控件", prompt)
            self.assertIn("状态栏文字不能算作功能实现", prompt)
            self.assertIn("QAction.trigger()", prompt)
            self.assertIn("临时目录", prompt)
            self.assertIn("QTextCursor.MoveMode.KeepAnchor", prompt)
        self.assertIn("{exec_stdout}", FIXER_PROMPT.template)
        self.assertIn("{exec_exit_code}", FIXER_PROMPT.template)
        self.assertIn("外层权限流程", FIXER_PROMPT.template)
        self.assertIn("{failure_analysis}", FIXER_PROMPT.template)
        self.assertIn("{exec_stdout}", DIAGNOSER_PROMPT.template)
        self.assertIn("{failure_focus_context}", DIAGNOSER_PROMPT.template)
        self.assertIn("逐步追踪状态变化", DIAGNOSER_PROMPT.template)
        self.assertIn("后续语句或方法调用", DIAGNOSER_PROMPT.template)
        self.assertIn("[LintWarning]", DIAGNOSER_PROMPT.template)

    def test_missing_pyqt_dependency_is_detected_for_permission_prompt(self) -> None:
        error = "ModuleNotFoundError: No module named 'PyQt5'"

        dependency = detect_missing_dependency(error)

        self.assertIsNotNone(dependency)
        self.assertEqual(dependency.module, "PyQt5")
        self.assertEqual(dependency.package, "PyQt5")
        self.assertTrue(is_install_approved("允许安装"))
        self.assertFalse(is_install_approved("暂时不要安装"))

    def test_preflight_detects_pyqt_import_hidden_by_fallback(self) -> None:
        code = (
            "try:\n"
            "    from PyQt5.QtWidgets import QApplication\n"
            "except ImportError:\n"
            "    print('文本降级')\n"
        )

        with patch("dependency_manager.importlib.util.find_spec", return_value=None):
            dependencies = detect_code_dependencies(code)

        self.assertEqual(dependencies, (MissingDependency("PyQt5", "PyQt5"),))

    def test_cv2_uses_reviewed_opencv_package_and_asks_in_ask_mode(self) -> None:
        with patch("dependency_manager.importlib.util.find_spec", return_value=None):
            report = dependency_manager.inspect_code_permissions("import cv2\n")

        self.assertEqual(
            report.missing_dependencies,
            (MissingDependency("cv2", "opencv-python"),),
        )
        self.assertEqual(
            dependency_manager.decide_permission_action(report, "ask").action,
            "ask",
        )

    def test_executor_blocks_missing_dependency_before_fallback_runs(self) -> None:
        state = CodeAgentState(
            user_requirement="写一个 PyQt5 登录界面",
            code=(
                "try:\n"
                "    from PyQt5.QtWidgets import QApplication\n"
                "except ImportError:\n"
                "    print('文本降级')\n"
            ),
        )
        with (
            patch("dependency_manager.importlib.util.find_spec", return_value=None),
            patch("graph_nodes.safe_execute_code", return_value=("文本降级\n", "")) as execute,
        ):
            updates = executor_node(state)

        self.assertIn("ModuleNotFoundError", updates["exec_stderr"])
        self.assertIn("DependencyPreflight", updates["exec_stderr"])
        execute.assert_not_called()

    def test_permission_levels_fail_closed_and_only_trust_allowlisted_installs(self) -> None:
        code = "from PyQt5.QtWidgets import QApplication\n"
        with patch("dependency_manager.importlib.util.find_spec", return_value=None):
            report = dependency_manager.inspect_code_permissions(code)

        self.assertEqual(
            dependency_manager.decide_permission_action(report, "restricted").action,
            "block",
        )
        self.assertEqual(
            dependency_manager.decide_permission_action(report, "ask").action,
            "ask",
        )
        self.assertEqual(
            dependency_manager.decide_permission_action(report, "trusted").action,
            "auto_install",
        )

    def test_sensitive_tools_are_listed_and_still_ask_in_trusted_mode(self) -> None:
        code = (
            "import requests\n"
            "from pathlib import Path\n"
            "requests.get('https://example.test')\n"
            "Path('result.txt').write_text('done', encoding='utf-8')\n"
        )
        with patch("dependency_manager.importlib.util.find_spec", return_value=object()):
            report = dependency_manager.inspect_code_permissions(code)

        capability_keys = {item.key for item in report.capabilities}
        self.assertEqual(capability_keys, {"network", "filesystem_write"})
        self.assertEqual(
            dependency_manager.decide_permission_action(report, "trusted").action,
            "ask",
        )

    def test_permission_report_names_camera_and_exact_file_operations(self) -> None:
        code = (
            "from pathlib import Path\n"
            "import cv2\n"
            "camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)\n"
            "Path('photos').mkdir(parents=True, exist_ok=True)\n"
            "cv2.imwrite(str(Path('photos') / 'shot.jpg'), frame)\n"
            "Path('temporary.txt').unlink()\n"
        )

        report = dependency_manager.inspect_code_permissions(code)
        capabilities = {item.key: item for item in report.capabilities}

        self.assertIn("camera_access", capabilities)
        self.assertIn("VideoCapture(0, cv2.CAP_DSHOW)", capabilities["camera_access"].detail)
        self.assertIn("Path('photos').mkdir", capabilities["filesystem_write"].detail)
        self.assertIn("cv2.imwrite", capabilities["filesystem_write"].detail)
        self.assertIn("temporary.txt", capabilities["filesystem_delete"].detail)

    def test_camera_contract_rejects_black_frame_blind_capture(self) -> None:
        from graph_nodes import validate_camera_capture_contract

        unsafe_code = (
            "import cv2\n"
            "cap = cv2.VideoCapture(0)\n"
            "ok, frame = cap.read()\n"
            "if ok:\n"
            "    cv2.imshow('preview', frame)\n"
        )
        robust_code = (
            "import sys\n"
            "import cv2\n"
            "def main():\n"
            "    if '--autocode-self-test' in sys.argv:\n"
            "        return 0\n"
            "    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):\n"
            "        cap = cv2.VideoCapture(0, backend)\n"
            "        for _ in range(20):\n"
            "            ok, frame = cap.read()\n"
            "        if ok and frame is not None and float(frame.mean()) > 2.0:\n"
            "            print(cap.getBackendName())\n"
            "            return 0\n"
            "        cap.release()\n"
            "    return 1\n"
        )

        unsafe_ok, unsafe_message = validate_camera_capture_contract(unsafe_code)
        robust_ok, robust_message = validate_camera_capture_contract(robust_code)

        self.assertFalse(unsafe_ok)
        self.assertIn("全黑", unsafe_message)
        self.assertTrue(robust_ok, robust_message)

    def test_execution_permission_context_detects_tampering(self) -> None:
        context = dependency_manager.create_execution_permission_context(
            "写一个 PyQt5 登录界面",
            "from PyQt5.QtWidgets import QApplication\n",
            "使用 PyQt5",
        )

        parsed = dependency_manager.parse_execution_permission_context(context)
        tampered = context.replace("PyQt5", "PyQt6")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.original_requirement, "写一个 PyQt5 登录界面")
        self.assertIsNone(dependency_manager.parse_execution_permission_context(tampered))

    @patch("dependency_manager.subprocess.run")
    def test_dependency_installer_uses_allowlist_and_never_invokes_a_shell(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Successfully installed PyQt5",
            stderr="",
        )

        result = install_dependency("PyQt5")
        rejected = install_dependency("unknown-package")

        self.assertTrue(result.success)
        self.assertFalse(rejected.success)
        command = run.call_args.args[0]
        self.assertIn("PyQt5", command)
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_dependency_install_requires_post_install_import_verification(self) -> None:
        pip_ok = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Successfully installed PyQt5",
            stderr="",
        )
        import_failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ImportError: DLL load failed while importing QtCore",
        )

        with (
            patch("dependency_manager.importlib.util.find_spec", return_value=None),
            patch(
                "dependency_manager.subprocess.run",
                side_effect=[pip_ok, import_failed],
            ) as run,
            patch("dependency_manager.importlib.invalidate_caches") as invalidate,
        ):
            result = install_dependency("PyQt5")

        self.assertFalse(result.success)
        self.assertIn("导入验证失败", result.message)
        self.assertIn("DLL load failed", result.message)
        self.assertEqual(run.call_count, 2)
        invalidate.assert_called_once_with()

    def test_hidden_pyqt_symbol_import_error_is_diagnosed_for_the_fixer(self) -> None:
        invalid_symbol = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "ImportError: cannot import name 'Signal' from "
                "'PyQt6.QtCore'"
            ),
        )

        with patch("dependency_manager.subprocess.run", return_value=invalid_symbol):
            diagnostic = dependency_manager.diagnose_code_imports(
                "from PyQt6.QtCore import Qt, Signal, pyqtSignal\n"
            )

        self.assertIn("PyQt6.QtCore", diagnostic)
        self.assertIn("Signal", diagnostic)
        self.assertIn("不是缺少 PyQt6 安装包", diagnostic)

    def test_executor_blocks_invalid_third_party_symbols_before_execution(self) -> None:
        state = CodeAgentState(
            user_requirement="写一个 PyQt 登录界面",
            code="from PyQt6.QtCore import Signal\nprint('ready')\n",
        )
        with (
            patch("graph_nodes.safe_execute_code") as execute,
            patch(
                "graph_nodes.diagnose_code_imports",
                return_value="- PyQt6.QtCore 中不存在 Signal",
            ),
        ):
            updates = executor_node(state)

        execute.assert_not_called()
        self.assertIn("ImportPreflight", updates["exec_stderr"])
        self.assertIn("不存在 Signal", updates["exec_stderr"])


class PersistenceTests(unittest.TestCase):
    def test_generated_code_can_be_saved_and_listed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("file_util.CODE_SAVE_DIR", output_dir):
                saved = save_code_to_file("print('saved')\n", phase="test")
                latest = get_latest_code_file()
                generated = get_all_generated_files()

            self.assertEqual(Path(saved).read_text(encoding="utf-8"), "print('saved')\n")
            self.assertEqual(latest, saved)
            self.assertEqual(generated, [saved])

    def test_memory_writes_sqlite_and_obsidian_notes_with_secret_redaction(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = MemoryStore(temp_dir)
            session_id = store.create_session("记忆测试")
            store.add_entry(
                session_id,
                "user",
                "我喜欢用 Obsidian，密钥是 sk-abcdefghijk123456",
            )
            store.remember(("用户喜欢在 Obsidian 查看对话日志",), session_id)
            recalled = store.recall(session_id)

            with closing(sqlite3.connect(store.db_path)) as connection:
                stored_content = connection.execute(
                    "SELECT content FROM entries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]

            note_text = next(store.sessions_dir.rglob("*.md")).read_text(encoding="utf-8")
            memory_text = store.memory_note_path.read_text(encoding="utf-8")

        self.assertIn("Obsidian", recalled)
        self.assertIn("[REDACTED_API_KEY]", stored_content)
        self.assertNotIn("sk-abcdefghijk123456", note_text)
        self.assertIn("用户喜欢在 Obsidian 查看对话日志", memory_text)

    def test_error_experience_is_recalled_only_after_a_verified_success(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = MemoryStore(temp_dir)
            session_id = store.create_session("PyQt 错误学习")
            failing_code = "from PyQt6.QtCore import Signal\n"
            fixed_code = "from PyQt6.QtCore import pyqtSignal\n"
            signature = store.record_error(
                session_id,
                "写一个 PyQt 登录界面",
                failing_code,
                "ImportError: cannot import name 'Signal' from 'PyQt6.QtCore'",
            )

            before_success = store.recall_error_experiences(
                "写一个 PyQt 登录界面",
                failing_code,
            )
            store.resolve_errors((signature,), fixed_code)
            after_success = store.recall_error_experiences(
                "再次写一个 PyQt 登录界面",
                failing_code,
            )

            with closing(sqlite3.connect(store.db_path)) as connection:
                row = connection.execute(
                    "SELECT occurrences, success_count, status FROM error_experiences"
                ).fetchone()

            error_notes = list(store.error_notes_dir.glob("*.md"))

        self.assertNotIn("Signal", before_success)
        self.assertIn("Signal", after_success)
        self.assertIn("pyqtSignal", after_success)
        self.assertEqual(row, (1, 1, "resolved"))
        self.assertEqual(len(error_notes), 1)


if __name__ == "__main__":
    unittest.main()
