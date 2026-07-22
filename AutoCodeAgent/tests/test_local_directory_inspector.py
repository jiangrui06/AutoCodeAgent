"""本地文件夹只读分析的回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_directory_inspector import find_requested_directory, summarize_directory


class LocalDirectoryInspectorTests(unittest.TestCase):
    def test_finds_existing_directory_before_trailing_instruction(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requested = Path(temp_dir) / "我的 简历"
            requested.mkdir()

            found = find_requested_directory(
                f"{requested} 分析一下这个文件夹下面的内容"
            )

        self.assertEqual(found, requested.resolve())

    def test_summary_reports_file_types_and_nested_contents(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requested = Path(temp_dir) / "简历"
            projects = requested / "projects"
            projects.mkdir(parents=True)
            (requested / "蒋睿简历.docx").write_bytes(b"resume")
            (requested / "简历备份.docx").write_bytes(b"backup")
            (projects / "format_resume.py").write_text("print('ok')", encoding="utf-8")

            summary = summarize_directory(requested)

        self.assertIn("3 个文件", summary)
        self.assertIn("1 个子目录", summary)
        self.assertIn(".docx：2", summary)
        self.assertIn(".py：1", summary)
        self.assertIn("蒋睿简历.docx", summary)
        self.assertIn("个人简历", summary)


if __name__ == "__main__":
    unittest.main()
