"""OpenHands 渐进式适配层的契约测试。"""

from __future__ import annotations

import unittest
import json
import subprocess
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from pathlib import Path
from uuid import UUID
from unittest.mock import patch

from openhands_adapter import (
    OPENHANDS_PERMISSION_PREFIX,
    OpenHandsRunResult,
    _analyze_pending_actions,
    _build_terminal_environment,
    _event_preview,
    _latest_artifact,
    _run_worker,
    _workspace_snapshot,
    build_openhands_llm_config,
    create_openhands_permission_context,
    execute_openhands_task,
    format_pending_actions,
    path_is_within_workspace,
    normalize_agent_engine,
    parse_openhands_permission_context,
    session_id_to_conversation_id,
    terminal_command_requires_confirmation,
)


class OpenHandsAdapterContractTests(unittest.TestCase):
    def test_terminal_environment_pins_python_and_pip_to_project_runtime(self) -> None:
        execution_python = Path(sys.executable).resolve()
        source = SimpleNamespace(effective_agent_execution_python=execution_python)

        with patch.dict("os.environ", {"PATH": "C:\\Windows\\System32"}):
            environment = _build_terminal_environment(source)

        path_entries = environment["PATH"].split(";")
        self.assertEqual(Path(path_entries[0]), execution_python.parent)
        self.assertEqual(environment["AUTOCODEAGENT_PYTHON"], str(execution_python))
        self.assertEqual(environment["PYTHONUTF8"], "1")

    def test_user_message_event_preview_hides_internal_context_and_private_paths(self) -> None:
        class MessageEvent:
            source = "user"

            def __str__(self) -> str:
                return (
                    "user: 分析附件 C:\\Users\\someone\\private.png "
                    "API_KEY=should-not-appear 长期记忆原文"
                )

        preview = _event_preview(MessageEvent())

        self.assertEqual(preview, "已接收用户需求（内部上下文已隐藏）")
        self.assertNotIn("private.png", preview)
        self.assertNotIn("should-not-appear", preview)

    def test_artifact_detection_reports_only_files_changed_by_the_current_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            stale = workspace / "old_result.py"
            stale.write_text("print('old')\n", encoding="utf-8")
            before = _workspace_snapshot(workspace)

            current = workspace / "permission_flow_probe.txt"
            current.write_text("PERMISSION_OK", encoding="utf-8")

            content, saved_path = _latest_artifact(workspace, before)

            self.assertEqual(content, "PERMISSION_OK")
            self.assertEqual(Path(saved_path), current)

    def test_artifact_detection_does_not_fall_back_to_an_old_workspace_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            stale = workspace / "old_result.py"
            stale.write_text("print('old')\n", encoding="utf-8")
            before = _workspace_snapshot(workspace)

            content, saved_path = _latest_artifact(workspace, before)

            self.assertEqual(content, "")
            self.assertEqual(saved_path, "")

    def test_engine_flag_fails_back_to_legacy(self) -> None:
        self.assertEqual(normalize_agent_engine("openhands"), "openhands")
        self.assertEqual(normalize_agent_engine(" OPENHANDS "), "openhands")
        self.assertEqual(normalize_agent_engine("unknown"), "legacy")
        self.assertEqual(normalize_agent_engine(""), "legacy")

    def test_sensenova_uses_openai_compatible_model_and_disables_reasoning(self) -> None:
        source = SimpleNamespace(
            llm_model="sensenova-6.7-flash-lite",
            llm_api_key="test-secret",
            base_url="https://token.sensenova.cn/v1/",
            llm_temperature=0.1,
            llm_max_tokens=4096,
            llm_timeout=90,
            llm_disable_reasoning=True,
        )

        config = build_openhands_llm_config(source)

        self.assertEqual(config.model, "openai/sensenova-6.7-flash-lite")
        self.assertEqual(config.base_url, "https://token.sensenova.cn/v1")
        self.assertEqual(config.api_key, "test-secret")
        self.assertEqual(config.litellm_extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(config.max_output_tokens, 4096)
        self.assertEqual(config.timeout, 90)
        self.assertTrue(config.force_vision)

    def test_existing_openai_prefix_is_not_duplicated(self) -> None:
        source = SimpleNamespace(
            llm_model="openai/custom-model",
            llm_api_key="key",
            base_url="https://example.test/v1",
            llm_temperature=0.0,
            llm_max_tokens=1024,
            llm_timeout=30,
            llm_disable_reasoning=False,
        )

        config = build_openhands_llm_config(source)

        self.assertEqual(config.model, "openai/custom-model")
        self.assertIsNone(config.litellm_extra_body)

    def test_session_id_conversion_is_stable_and_always_a_uuid(self) -> None:
        direct = session_id_to_conversation_id("a" * 32)
        derived = session_id_to_conversation_id("session-one")

        self.assertIsInstance(direct, UUID)
        self.assertEqual(direct.hex, "a" * 32)
        self.assertEqual(derived, session_id_to_conversation_id("session-one"))
        self.assertNotEqual(derived, session_id_to_conversation_id("session-two"))

    def test_permission_context_round_trips_and_rejects_tampering(self) -> None:
        conversation_id = session_id_to_conversation_id("session-one")
        actions = (
            SimpleNamespace(
                tool_name="terminal",
                summary="安装项目依赖",
                security_risk="HIGH",
                action=SimpleNamespace(command="python -m pip install demo"),
            ),
        )

        context = create_openhands_permission_context(
            conversation_id,
            "创建并测试应用",
            actions,
        )
        parsed = parse_openhands_permission_context(context)

        self.assertTrue(context.startswith(OPENHANDS_PERMISSION_PREFIX))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.conversation_id, conversation_id)
        self.assertEqual(parsed.original_requirement, "创建并测试应用")
        self.assertIn("terminal", parsed.action_summaries[0])
        self.assertIn("安装项目依赖", parsed.action_summaries[0])

        replacement = "0" if context[-1] != "0" else "1"
        self.assertIsNone(parse_openhands_permission_context(context[:-1] + replacement))

    def test_pending_action_display_names_exact_tools_and_risk(self) -> None:
        actions = (
            SimpleNamespace(
                tool_name="file_editor",
                summary="修改配置文件",
                security_risk="MEDIUM",
                action=SimpleNamespace(path="settings.py", command="str_replace"),
            ),
        )

        text = format_pending_actions(actions)

        self.assertIn("`file_editor`", text)
        self.assertIn("修改配置文件", text)
        self.assertIn("MEDIUM", text)
        self.assertIn("settings.py", text)

    def test_new_task_pauses_with_a_signed_permission_context(self) -> None:
        pending_action = SimpleNamespace(
            tool_name="terminal",
            summary="运行测试",
            security_risk="LOW",
            action=SimpleNamespace(command="python -m unittest"),
        )
        state = SimpleNamespace(
            execution_status=SimpleNamespace(value="waiting_for_confirmation"),
            events=[pending_action],
        )
        conversation = _FakeConversation(state, pending=(pending_action,))

        with TemporaryDirectory() as temp_dir, patch(
            "openhands_adapter._create_conversation",
            return_value=(conversation, Path(temp_dir)),
        ):
            result = execute_openhands_task(
                "修复并运行测试",
                "session-one",
                "ask",
                source=SimpleNamespace(),
            )

        self.assertEqual(conversation.sent, ["修复并运行测试"])
        self.assertEqual(conversation.run_count, 1)
        self.assertTrue(conversation.closed)
        self.assertEqual(result.status, "waiting_for_confirmation")
        self.assertTrue(result.pending_context.startswith(OPENHANDS_PERMISSION_PREFIX))
        self.assertIn("运行测试", result.markdown)

    def test_approval_resumes_without_duplicating_the_user_message(self) -> None:
        state = SimpleNamespace(
            execution_status=SimpleNamespace(value="finished"),
            events=[SimpleNamespace(source="agent", llm_message=None)],
        )
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            generated = workspace / "done.py"
            conversation = _FakeConversation(
                state,
                on_run=lambda: generated.write_text(
                    "print('done')\n",
                    encoding="utf-8",
                ),
            )
            with patch(
                "openhands_adapter._create_conversation",
                return_value=(conversation, workspace),
            ):
                result = execute_openhands_task(
                    "修复并运行测试",
                    "session-one",
                    "ask",
                    decision="approve",
                    source=SimpleNamespace(),
                )

        self.assertEqual(conversation.sent, [])
        self.assertEqual(conversation.run_count, 1)
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.code, "print('done')\n")
        self.assertTrue(result.saved_path.endswith("done.py"))

    def test_stale_approval_does_not_execute_a_changed_pending_action(self) -> None:
        current_action = SimpleNamespace(
            tool_name="terminal",
            summary="run a different command",
            security_risk="LOW",
            action=SimpleNamespace(command="python changed.py"),
        )
        state = SimpleNamespace(
            execution_status=SimpleNamespace(value="waiting_for_confirmation"),
            events=[current_action],
            security_analyzer=None,
        )
        conversation = _FakeConversation(state, pending=(current_action,))

        with TemporaryDirectory() as temp_dir, patch(
            "openhands_adapter._create_conversation",
            return_value=(conversation, Path(temp_dir)),
        ):
            result = execute_openhands_task(
                "run tests",
                "session-one",
                "ask",
                decision="approve",
                expected_action_summaries=("previous signed action",),
                source=SimpleNamespace(),
            )

        self.assertEqual(conversation.run_count, 0)
        self.assertEqual(result.status, "waiting_for_confirmation")
        self.assertEqual(len(result.action_summaries), 1)
        self.assertIn("different command", result.markdown)
        self.assertTrue(result.pending_context.startswith(OPENHANDS_PERMISSION_PREFIX))

    def test_rejection_records_observation_without_running_pending_action(self) -> None:
        state = SimpleNamespace(
            execution_status=SimpleNamespace(value="waiting_for_confirmation"),
            events=[],
        )
        conversation = _FakeConversation(state)
        with TemporaryDirectory() as temp_dir, patch(
            "openhands_adapter._create_conversation",
            return_value=(conversation, Path(temp_dir)),
        ):
            result = execute_openhands_task(
                "删除文件",
                "session-one",
                "ask",
                decision="reject",
                source=SimpleNamespace(),
            )

        self.assertEqual(conversation.rejections, ["用户在 AutoCodeAgent 中拒绝了本次操作"])
        self.assertEqual(conversation.run_count, 0)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(conversation.closed)

    @patch("openhands_adapter.subprocess.run")
    def test_worker_transport_uses_stdin_json_without_a_shell(self, run) -> None:
        worker_result = OpenHandsRunResult(
            status="finished",
            markdown="完成",
            event_count=3,
        )
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "runtime log\nAUTOCODE_OPENHANDS_RESULT="
                + json.dumps(worker_result.__dict__, ensure_ascii=False)
                + "\n"
            ),
            stderr="",
        )
        source = SimpleNamespace(
            effective_openhands_python=Path(sys.executable),
            openhands_worker_timeout=120,
        )

        result = _run_worker({"requirement": "测试"}, source)

        self.assertEqual(result.status, "finished")
        self.assertEqual(result.event_count, 3)
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(json.loads(kwargs["input"]), {"requirement": "测试"})
        self.assertIn("-I", run.call_args.args[0])

    def test_workspace_boundary_detects_outside_paths_and_risky_shell_forms(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            inside = workspace / "src" / "app.py"
            outside = Path(temp_dir) / "outside.txt"

            self.assertTrue(path_is_within_workspace(inside, workspace))
            self.assertTrue(path_is_within_workspace("src/app.py", workspace))
            self.assertFalse(path_is_within_workspace(outside, workspace))
            self.assertFalse(path_is_within_workspace("../outside.txt", workspace))
            self.assertFalse(
                terminal_command_requires_confirmation("python -m unittest", workspace)
            )
            self.assertFalse(
                terminal_command_requires_confirmation("pwd; ls -la", workspace)
            )
            self.assertTrue(
                terminal_command_requires_confirmation(
                    f"Set-Content -Path '{outside}' -Value unsafe",
                    workspace,
                )
            )
            self.assertTrue(
                terminal_command_requires_confirmation("cd ..; Remove-Item x", workspace)
            )
            self.assertTrue(
                terminal_command_requires_confirmation("python -m pip install demo", workspace)
            )

    def test_pending_actions_show_the_deterministic_analyzer_risk(self) -> None:
        class FakeAction:
            security_risk = "UNKNOWN"

            def model_copy(self, update):
                copied = FakeAction()
                copied.security_risk = update["security_risk"]
                return copied

        analyzer = SimpleNamespace(security_risk=lambda _action: "HIGH")
        conversation = SimpleNamespace(
            state=SimpleNamespace(security_analyzer=analyzer)
        )

        analyzed = _analyze_pending_actions(conversation, (FakeAction(),))

        self.assertEqual(analyzed[0].security_risk, "HIGH")

    def test_uploaded_image_is_encoded_as_a_bounded_data_url(self) -> None:
        from openhands_adapter import image_paths_to_data_urls

        with TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir) / "uploads"
            upload_root.mkdir()
            image_path = upload_root / "probe.png"
            image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
            image_path.write_bytes(image_bytes)

            urls = image_paths_to_data_urls([image_path], upload_root)

            self.assertEqual(len(urls), 1)
            self.assertTrue(urls[0].startswith("data:image/png;base64,"))

            outside = Path(temp_dir) / "outside.png"
            outside.write_bytes(image_bytes)
            with self.assertRaises(ValueError):
                image_paths_to_data_urls([outside], upload_root)

    def test_image_task_sends_a_multimodal_message(self) -> None:
        state = SimpleNamespace(
            execution_status=SimpleNamespace(value="finished"),
            events=[],
        )
        conversation = _FakeConversation(state)
        multimodal_message = object()
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "stale.py").write_text("old", encoding="utf-8")
            with patch(
                "openhands_adapter._create_conversation",
                return_value=(conversation, workspace),
            ), patch(
                "openhands_adapter._build_openhands_user_message",
                return_value=multimodal_message,
                create=True,
            ) as build_message:
                result = execute_openhands_task(
                    "describe image",
                    "image-session",
                    "ask",
                    image_paths=["C:/uploads/probe.png"],
                    allow_tools=False,
                    source=SimpleNamespace(),
                )

        build_message.assert_called_once_with(
            "describe image", (str(Path("C:/uploads/probe.png")),), SimpleNamespace()
        )
        self.assertEqual(conversation.sent, [multimodal_message])
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.saved_path, "")

    @patch("openhands_adapter._run_worker")
    def test_image_paths_are_forwarded_to_the_isolated_worker(self, run_worker) -> None:
        run_worker.return_value = OpenHandsRunResult(
            status="finished",
            markdown="done",
        )

        execute_openhands_task(
            "describe image",
            "image-session",
            "ask",
            image_paths=["C:/uploads/probe.png"],
            allow_tools=False,
        )

        payload = run_worker.call_args.args[0]
        self.assertEqual(payload["image_paths"], [str(Path("C:/uploads/probe.png"))])
        self.assertFalse(payload["allow_tools"])


class _FakeConversation:
    def __init__(self, state, pending=(), on_run=None):
        self.state = state
        self.pending = tuple(pending)
        self.on_run = on_run
        self.sent = []
        self.run_count = 0
        self.rejections = []
        self.closed = False

    def send_message(self, message):
        self.sent.append(message)

    def run(self):
        self.run_count += 1
        if self.on_run:
            self.on_run()

    def reject_pending_actions(self, reason):
        self.rejections.append(reason)

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
