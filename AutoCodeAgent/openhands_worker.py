"""OpenHands 隔离运行时入口；只接受 stdin JSON，只输出带标记的结果 JSON。"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import settings  # noqa: E402
from openhands_adapter import (  # noqa: E402
    OpenHandsRunResult,
    _execute_openhands_in_process,
    serialize_worker_result,
)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        result = _execute_openhands_in_process(
            str(payload["requirement"]),
            str(payload["session_id"]),
            str(payload["permission_level"]),
            decision=str(payload.get("decision", "start")),
            conversation_id=uuid.UUID(str(payload["conversation_id"])),
            expected_action_summaries=(
                tuple(str(item) for item in payload["expected_action_summaries"])
                if payload.get("expected_action_summaries") is not None
                else None
            ),
            source=settings,
            sign_pending_context=False,
            image_paths=tuple(str(item) for item in payload.get("image_paths", ())),
            allow_tools=bool(payload.get("allow_tools", True)),
        )
        exit_code = 0
    except Exception as exc:
        result = OpenHandsRunResult(
            status="error",
            markdown=(
                "## OpenHands 独立运行时出错\n\n"
                f"**错误类型：** `{type(exc).__name__}`\n\n"
                f"```text\n{exc}\n```"
            ),
            error=f"{type(exc).__name__}: {exc}",
        )
        exit_code = 1
    print(serialize_worker_result(result), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
