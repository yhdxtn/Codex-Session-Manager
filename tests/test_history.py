from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from codex_history_viewer.exporter import export_migration_bundle, export_one
from codex_history_viewer.importer import _safe_relative_path, import_archives
from codex_history_viewer.reader import scan_codex_home, load_session_events


def dump_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def envelope(record_type: str, payload: object, ordinal: int, timestamp: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": record_type, "timestamp": timestamp, "ordinal": ordinal, "payload": payload}


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "codex"
        self.root.mkdir()
        self.out = Path(self.temp.name) / "exports"
        self.out.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_chat_fixture(self) -> tuple[str, Path]:
        sid = "11111111-1111-4111-8111-111111111111"
        session_dir = self.root / "sessions" / "2026" / "01" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        user = {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "你好，密码=TopSecret123"}, {"type": "input_image", "image_url": "C:\\not-found\\api_key=IssueSecret"}]}
        call = {"type": "custom_tool_call", "id": "tool1", "call_id": "call-1", "name": "exec_command", "input": "echo hello"}
        output = {"type": "custom_tool_call_output", "id": "out1", "call_id": "call-1", "output": [{"type": "output_text", "text": "stdout: hello"}]}
        final = {"type": "message", "id": "a1", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "结果 <script>alert(1)</script> 完成"}]}
        lines = [
            envelope("session_meta", {"session_id": sid, "timestamp": "2026-01-01T00:00:00Z", "cwd": "C:\\repo"}, 0),
            envelope("response_item", user, 1),
            envelope("response_item", call, 2),
            envelope("event_msg", {"type": "item_completed", "item": call}, 3),
            envelope("response_item", output, 4),
            envelope("response_item", final, 5, "2026-01-01T00:02:00Z"),
            envelope("response_item", final, 6, "2026-01-01T00:02:00Z"),
            envelope("turn_aborted", {"reason": "sample"}, 7),
        ]
        with path.open("w", encoding="utf-8", newline="") as stream:
            for item in lines:
                stream.write(dump_line(item))
            stream.write("{broken json line\n")
        folder = self.root / "attachments" / sid
        folder.mkdir(parents=True)
        (folder / "pasted-text.txt").write_text("pasted text\napi_key=AttachmentSecret", encoding="utf-8")
        (self.root / "attachments" / "unrelated-session" ).mkdir()
        (self.root / "attachments" / "unrelated-session" / "private.txt").write_text("not related")
        return sid, path

    def test_constructed_archive_duplicate_damage_attachment_and_exports(self) -> None:
        sid, raw = self.write_chat_fixture()
        archive_id = "22222222-2222-4222-8222-222222222222"
        archived = self.root / "archived_sessions"
        archived.mkdir()
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard dependency is not installed")
        data = dump_line(envelope("session_meta", {"session_id": archive_id, "timestamp": "2025-12-31T23:59:00Z", "cwd": "C:\\archived"}, 0))
        data += dump_line(envelope("response_item", {"type": "message", "id": "arch-u", "role": "user", "content": [{"type": "input_text", "text": "归档会话"}]}, 1))
        compressed = zstandard.ZstdCompressor().compress(data.encode("utf-8"))
        archive_path = archived / f"rollout-2025-12-31T23-59-00-{archive_id}.jsonl.zst"
        archive_path.write_bytes(compressed)

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        chat = next(item for item in catalog.sessions if item.session_id == sid)
        archive = next(item for item in catalog.sessions if item.session_id == archive_id)
        self.assertEqual(chat.title[:2], "你好")
        self.assertEqual(chat.user_count, 1)
        self.assertEqual(chat.assistant_count, 1)
        self.assertEqual(chat.tool_count, 1)
        self.assertGreaterEqual(chat.duplicate_count, 1)
        self.assertTrue(any("损坏" in value for value in chat.issues))
        self.assertTrue(chat.has_attachment)
        self.assertTrue(archive.archived)
        self.assertEqual(archive.user_count, 1)

        events, issues, _ = load_session_events(chat, catalog)
        visible = [event for event in events if event.get("kind") == "message"]
        self.assertEqual(len(visible), 2)
        call = next(event for event in events if event.get("kind") == "tool_call")
        result = next(event for event in events if event.get("kind") == "tool_result")
        self.assertEqual(call["call_id"], result["call_id"])
        self.assertTrue(any("未找到" in value for value in issues))

        export = export_one(chat, catalog, str(self.out), ["txt", "md", "html", "json", "zip", "raw"], "full", True, True, threading.Event())
        self.assertTrue(export["status"].startswith("完成"))
        folder = Path(export["folder"])
        raw_copy = next((folder / "原始文件").glob("*.jsonl"))
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), hashlib.sha256(raw_copy.read_bytes()).hexdigest())
        html_text = (folder / "聊天记录.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", html_text)
        self.assertNotIn("<script>alert", html_text)
        json_text = (folder / "聊天记录.json").read_text(encoding="utf-8")
        self.assertIn("[已脱敏:", json_text)
        self.assertIn("读取报告", html_text)
        self.assertIn("read_issues", json_text)
        for filename in ("聊天记录.txt", "聊天记录.md", "聊天记录.html", "聊天记录.json"):
            exported_text = (folder / filename).read_text(encoding="utf-8")
            self.assertIn("read_issues" if filename.endswith(".json") else "读取报告", exported_text)
            self.assertNotIn("IssueSecret", exported_text)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("IssueSecret", json.dumps(manifest, ensure_ascii=False))
        self.assertGreaterEqual(manifest["redacted_occurrences"], 1)
        self.assertFalse(any("unrelated-session" in item for item in (p.as_posix() for p in folder.rglob("*"))))
        with zipfile.ZipFile(export["zip"]) as archive_file:
            self.assertTrue(archive_file.testzip() is None)
            self.assertTrue(any(name.endswith("manifest.json") for name in archive_file.namelist()))

    def test_database_wal_snapshot_and_database_only_items(self) -> None:
        import sqlite3

        sid = "db-only-session"
        db_path = self.root / "state_records.sqlite"
        writer = sqlite3.connect(db_path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE conversation_metadata(session_id TEXT, display_title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, cwd TEXT, archived INTEGER)")
        writer.execute("CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT, item_type TEXT)")
        writer.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?,?,?)", (sid, "数据库中文会话", 1767225600000, 1767225660000, "C:\\db", 1))
        item = {"type": "commandExecution", "id": "cmd-1", "command": "echo WAL", "cwd": "C:\\db", "aggregatedOutput": "stdout: WAL visible", "exitCode": 0, "status": "completed"}
        writer.execute("INSERT INTO conversation_items VALUES(?,?,?,?)", (sid, 1, json.dumps(item), "commandExecution"))
        writer.commit()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.title, "数据库中文会话")
        self.assertTrue(session.archived)
        self.assertEqual(session.source, "SQLite 条目回退")
        events, issues, _ = load_session_events(session, catalog)
        self.assertEqual([event["kind"] for event in events], ["tool_call", "tool_result"])
        self.assertIn("stdout: WAL visible", events[1]["text"])
        self.assertEqual(events[0]["call_id"], events[1]["call_id"])
        writer.close()

    def test_large_jsonl_is_streamed_and_unknown_type_reported(self) -> None:
        sid = "33333333-3333-4333-8333-333333333333"
        session_dir = self.root / "sessions" / "2026" / "02" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-02-01T00-00-00-{sid}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            stream.write(dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-02-01T00:00:00Z"}, 0)))
            for i in range(20_000):
                stream.write(dump_line(envelope("event_msg", {"type": "token_count", "sample": i}, i + 1)))
            stream.write(dump_line(envelope("future_record_v99", {"payload": "kept"}, 20_001)))
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.event_count, 20_002)
        self.assertTrue(any("未知记录类型" in issue for issue in session.issues))
        events, _, _ = load_session_events(session, catalog)
        self.assertTrue(any(event.get("name") == "future_record_v99" for event in events))

    def test_referenced_codex_attachment_folder_is_exported_without_neighbor_folders(self) -> None:
        sid = "44444444-4444-4444-8444-444444444444"
        attachment_id = "55555555-5555-4555-8555-555555555555"
        attachment_dir = self.root / "attachments" / attachment_id
        attachment_dir.mkdir(parents=True)
        (attachment_dir / "pasted-text.txt").write_text("api_key=AttachmentSecret", encoding="utf-8")
        unrelated = self.root / "attachments" / "66666666-6666-4666-8666-666666666666"
        unrelated.mkdir()
        (unrelated / "private.txt").write_text("must not be exported", encoding="utf-8")

        session_dir = self.root / "sessions" / "2026" / "03" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-03-01T00-00-00-{sid}.jsonl"
        message = {
            "type": "message",
            "id": "user-attachment",
            "role": "user",
            "content": [{"type": "input_text", "text": f"附件：{attachment_dir / 'pasted-text.txt'}"}],
        }
        raw.write_text(
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-03-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", message, 1)),
            encoding="utf-8",
        )

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertTrue(session.has_attachment)
        self.assertNotIn("attachment_ids", session.public_dict())

        events, issues, _ = load_session_events(session, catalog)
        self.assertFalse(issues)
        linked = next(event for event in events if event.get("role") == "user")
        self.assertEqual([item["ref"] for item in linked["attachments"]], [str(attachment_dir / "pasted-text.txt")])
        self.assertEqual(linked["attachments"][0]["status"], "本地文件存在")

        result = export_one(session, catalog, str(self.out), ["md", "zip"], "chat", True, False, threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        exported_files = list((Path(result["folder"]) / "attachments").glob("*"))
        self.assertEqual(len(exported_files), 1)
        self.assertTrue(exported_files[0].name.startswith("pasted-text-"))
        copied_text = exported_files[0].read_text(encoding="utf-8")
        self.assertIn("[已脱敏:", copied_text)
        self.assertNotIn("AttachmentSecret", copied_text)
        markdown = (Path(result["folder"]) / "聊天记录.md").read_text(encoding="utf-8")
        self.assertIn("[附件：Codex 附件](attachments/", markdown)
        self.assertNotIn("![Codex 附件]", markdown)
        txt_result = export_one(session, catalog, str(self.out), ["txt"], "chat", False, False, threading.Event())
        self.assertIn("[附件] attachments/", (Path(txt_result["folder"]) / "聊天记录.txt").read_text(encoding="utf-8"))
        self.assertFalse(any(path.name == "private.txt" for path in Path(result["folder"]).rglob("*")))

    def test_migration_bundle_import_is_visible_and_keeps_attachments(self) -> None:
        sid = "77777777-7777-4777-8777-777777777777"
        attachment_dir = self.root / "attachments" / sid
        attachment_dir.mkdir(parents=True)
        attachment = attachment_dir / "reference.txt"
        attachment.write_text("迁移后仍可读取的附件", encoding="utf-8")

        session_dir = self.root / "sessions" / "2026" / "04" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-04-01T00-00-00-{sid}.jsonl"
        user = {
            "type": "message",
            "id": "user-1",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "请查看这个附件"},
                {"type": "input_image", "image_url": str(attachment)},
            ],
        }
        assistant = {
            "type": "message",
            "id": "assistant-1",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "这是可跨电脑查看的测试会话"}],
        }
        raw.write_text(
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-04-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", user, 1))
            + dump_line(envelope("response_item", assistant, 2)),
            encoding="utf-8",
        )

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        source_session = next(item for item in catalog.sessions if item.session_id == sid)
        bundle_path = self.out / "cross-computer-migration.zip"
        exported = export_migration_bundle([source_session], catalog, str(bundle_path), threading.Event())
        self.assertEqual(exported["exported"], 1)
        self.assertTrue(bundle_path.is_file())
        with zipfile.ZipFile(bundle_path) as bundle:
            self.assertIsNone(bundle.testzip())

        import_root = Path(self.temp.name) / "destination-computer" / "imports"
        imported_result = import_archives([str(bundle_path)], str(import_root))
        self.assertEqual(imported_result["imported"], 1)
        self.assertEqual(imported_result["failed"], 0)
        self.assertEqual(len(imported_result["sessions"]), 1)

        imported_session = imported_result["sessions"][0]
        self.assertEqual(imported_session.session_id, sid)
        self.assertEqual(imported_session.source, "导入 ZIP")
        self.assertIn("这是可跨电脑查看的测试会话", imported_session.search_text)
        events, issues, _ = load_session_events(imported_session, catalog)
        self.assertFalse(issues)
        user_event = next(event for event in events if event.get("role") == "user")
        imported_attachment = Path(user_event["attachments"][0]["ref"])
        self.assertEqual(imported_attachment.read_text(encoding="utf-8"), "迁移后仍可读取的附件")

        duplicate_result = import_archives([str(bundle_path)], str(import_root), {sid})
        self.assertEqual(duplicate_result["imported"], 0)
        self.assertEqual(duplicate_result["skipped"], 1)
        self.assertEqual(duplicate_result["failed"], 0)

    def test_import_paths_are_safe_for_windows_targets(self) -> None:
        for unsafe in (
            "attachments/../outside.txt",
            "attachments/C:/outside.txt",
            "attachments/token.txt:secret",
            "attachments/CON.txt",
            "attachments/trailing-dot.",
            "attachments/trailing-space ",
        ):
            with self.subTest(path=unsafe):
                self.assertEqual(_safe_relative_path(unsafe), "")
        self.assertEqual(
            _safe_relative_path(r"attachments\images\sample.png"),
            "attachments/images/sample.png",
        )


if __name__ == "__main__":
    unittest.main()

