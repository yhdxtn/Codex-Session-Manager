from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

APP_VERSION = "1.1.6"
SEARCH_LIMIT = 256_000
STATUS_RECORD_TYPES = {"error", "task_started", "task_complete", "turn_aborted", "turn_interrupted", "turn_failed", "turn_cancelled", "turn_abandoned", "exec_command_begin", "exec_command_end"}
ATTACHMENT_FOLDER_RE = re.compile(r"(?i)(?:^|[\\/])attachments[\\/](?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\\/]")


@dataclass
class SessionInfo:
    session_id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    raw_created_at: str = ""
    raw_updated_at: str = ""
    cwd: str = ""
    archived: bool = False
    user_count: int = 0
    assistant_count: int = 0
    tool_count: int = 0
    event_count: int = 0
    source: str = ""
    status: str = "未扫描"
    paths: list[str] = field(default_factory=list)
    search_text: str = ""
    type_counts: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    has_attachment: bool = False
    attachment_ids: set[str] = field(default_factory=set)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("search_text", None)
        data.pop("attachment_ids", None)
        return data


@dataclass
class TableSchema:
    name: str
    columns: list[str]
    metadata_id: str = ""
    title: str = ""
    item_json: str = ""
    item_type: str = ""


@dataclass
class DatabaseSnapshot:
    source_path: str
    snapshot_path: str
    tables: list[TableSchema] = field(default_factory=list)
    error: str = ""


@dataclass
class Catalog:
    root: str
    sessions: list[SessionInfo] = field(default_factory=list)
    databases: list[DatabaseSnapshot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    temp_dir: str = ""
    files_scanned: int = 0
    bytes_scanned: int = 0


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _readonly_uri(path: str) -> str:
    uri = Path(path).resolve().as_uri()
    return uri + ("&" if "?" in uri else "?") + "mode=ro"


def _connect_readonly(path: str) -> sqlite3.Connection:
    return sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0)


def _candidate_columns(table: str, columns: Iterable[str]) -> TableSchema:
    original = list(columns)
    lowered = {c.casefold(): c for c in original}
    table_info = TableSchema(name=table, columns=original)
    id_col = next((lowered[k] for k in ("thread_id", "session_id", "id") if k in lowered), "")
    title_col = next((lowered[k] for k in ("title", "display_title", "thread_name") if k in lowered), "")
    time_cols = {"created_at", "created_at_ms", "updated_at", "updated_at_ms", "source_created_at", "source_updated_at", "rollout_path", "cwd"}
    metadata_like = bool(id_col and title_col and time_cols.intersection(lowered) and ("rollout_path" in lowered or "thread_id" in lowered or "session_id" in lowered or "cwd" in lowered))
    if not metadata_like and "rollout_path" in lowered and id_col:
        metadata_like = True
    table_info.metadata_id = id_col if metadata_like else ""
    table_info.title = title_col if metadata_like else ""
    json_col = next((c for k, c in lowered.items() if k.endswith("_json") and k not in {"error_json"}), "")
    thread_key = lowered.get("thread_id") or lowered.get("session_id")
    if thread_key and json_col:
        table_info.item_json = json_col
        table_info.item_type = next((lowered[k] for k in ("item_type", "record_type", "type") if k in lowered), "")
    return table_info


def _open_database_snapshot(source: str, temp_dir: str) -> DatabaseSnapshot:
    snapshot = DatabaseSnapshot(source_path=source, snapshot_path="")
    src_conn: sqlite3.Connection | None = None
    try:
        src_conn = _connect_readonly(source)
        names = src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        schemas: list[TableSchema] = []
        for (table,) in names:
            rows = src_conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
            schemas.append(_candidate_columns(table, (r[1] for r in rows)))
        snapshot.tables = schemas
        if not any(s.metadata_id or s.item_json for s in schemas):
            snapshot.error = "未发现符合会话元数据或记录条目结构的表"
            src_conn.close()
            return snapshot

        key = hashlib.sha256(os.path.abspath(source).encode("utf-8", "replace")).hexdigest()[:16]
        dest_path = os.path.join(temp_dir, key + ".sqlite")
        dest_conn = sqlite3.connect(dest_path)
        try:
            src_conn.backup(dest_conn, pages=512, sleep=0.02)
        finally:
            dest_conn.close()
        snapshot.snapshot_path = dest_path
    except Exception as exc:
        snapshot.error = f"SQLite 只读快照失败：{type(exc).__name__}: {exc}"
    finally:
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass
    return snapshot


def _epoch_string(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _time_sort_key(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        try:
            return float(raw)
        except ValueError:
            return 0.0


def _local_display(raw: str) -> str:
    if not raw:
        return ""
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def _decode_zstd(path: str):
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("缺少 zstandard 依赖，无法读取归档 JSONL.ZST") from exc
    raw = open(path, "rb")
    reader = zstandard.ZstdDecompressor().stream_reader(raw)
    return io.TextIOWrapper(reader, encoding="utf-8", errors="replace"), raw


def _iter_jsonl(path: str):
    if path.casefold().endswith(".zst"):
        text_stream, raw = _decode_zstd(path)
        try:
            for line_no, line in enumerate(text_stream, 1):
                yield line_no, line
        finally:
            text_stream.close()
            raw.close()
    else:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            for line_no, line in enumerate(stream, 1):
                yield line_no, line


def _content_text(value: Any, attachments: list[dict[str, Any]] | None = None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                kind = str(entry.get("type", ""))
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)
                if "image" in kind.casefold() or "file" in kind.casefold():
                    ref = entry.get("image_url") or entry.get("url") or entry.get("path") or entry.get("file_path")
                    if ref:
                        if attachments is not None:
                            attachments.append({"ref": ref, "kind": kind or "附件"})
                        parts.append(f"[[ATTACHMENT:{ref}]]")
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _collect_attachment_refs(value: Any, output: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in {"image_url", "image_path", "file_path", "attachment_path", "local_image_path"} and isinstance(child, str):
                output.append({"ref": child, "kind": key})
            else:
                _collect_attachment_refs(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_attachment_refs(child, output)


def _codex_attachment_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group("id") for match in ATTACHMENT_FOLDER_RE.finditer(value))
    elif isinstance(value, dict):
        for child in value.values():
            found.update(_codex_attachment_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_codex_attachment_ids(child))
    return found


def _append_codex_attachment_refs(value: Any, root: str, output: list[dict[str, Any]], session: SessionInfo | None = None) -> None:
    attachment_root = Path(root) / "attachments"
    root_real = attachment_root.resolve()
    existing = {str(item.get("ref", "")) for item in output}
    for attachment_id in _codex_attachment_ids(value):
        if session is not None:
            session.attachment_ids.add(attachment_id)
        folder = attachment_root / attachment_id
        if not folder.is_dir() or folder.is_symlink():
            reference = str(folder)
            if reference not in existing:
                output.append({"ref": reference, "kind": "Codex 附件"})
                existing.add(reference)
            continue
        found_file = False
        for directory, directories, filenames in os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if not os.path.islink(os.path.join(directory, name))]
            for filename in filenames:
                path = Path(directory) / filename
                if path.is_symlink():
                    continue
                try:
                    resolved = path.resolve()
                    if os.path.commonpath((os.path.normcase(str(root_real)), os.path.normcase(str(resolved)))) != os.path.normcase(str(root_real)):
                        continue
                except (OSError, ValueError):
                    continue
                reference = str(path)
                found_file = True
                if reference not in existing:
                    output.append({"ref": reference, "kind": "Codex 附件"})
                    existing.add(reference)
        if not found_file:
            reference = str(folder)
            if reference not in existing:
                output.append({"ref": reference, "kind": "Codex 附件"})
                existing.add(reference)


def _normalized_response(payload: dict[str, Any], envelope: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    kind = str(payload.get("type", "unknown"))
    timestamp = str(envelope.get("timestamp", ""))
    ordinal = envelope.get("ordinal")
    item_id = str(payload.get("id", ""))
    shared = {"timestamp": timestamp, "raw_timestamp": timestamp, "ordinal": ordinal, "item_id": item_id, "source_type": kind}
    if kind in {"message", "agentMessage", "userMessage"}:
        role = str(payload.get("role", ""))
        if not role:
            role = "user" if kind == "userMessage" else "assistant"
        phase = str(payload.get("phase", ""))
        if role not in {"user", "assistant"} or phase.casefold() in {"analysis", "justify", "confidence", "delegate_summary", "juicepred"}:
            return None
        text = _content_text(payload.get("content", payload.get("text", "")), attachments)
        return {**shared, "kind": "message", "role": role, "phase": phase, "text": text, "data": payload}
    if kind in {"function_call", "custom_tool_call", "mcp_tool_call", "dynamic_tool_call", "web_search_call", "computer_call"} or (kind.endswith("_call") and kind != "message"):
        tool_name = payload.get("name") or payload.get("tool") or payload.get("namespace") or kind
        tool_input = payload.get("input", payload.get("arguments", payload.get("query", "")))
        text = _content_text(tool_input)
        return {**shared, "kind": "tool_call", "role": "tool", "name": str(tool_name), "call_id": payload.get("call_id") or item_id, "text": text, "data": payload}
    if kind in {"function_call_output", "custom_tool_call_output", "mcp_tool_call_output"} or kind.endswith("_output"):
        output = payload.get("output", payload.get("content", payload.get("result", "")))
        return {**shared, "kind": "tool_result", "role": "tool", "name": kind, "call_id": payload.get("call_id", ""), "text": _content_text(output, attachments), "data": payload}
    if kind == "reasoning":
        return None
    if kind in {"web_search_result", "image_generation_call", "file_search_call"}:
        return {**shared, "kind": "tool_call", "role": "tool", "name": kind, "call_id": payload.get("call_id", item_id), "text": _content_text(payload), "data": payload}
    _collect_attachment_refs(payload, attachments)
    return {**shared, "kind": "unknown", "role": "event", "name": kind, "text": _content_text(payload), "data": payload}


def _normalized_database_items(payload: dict[str, Any], envelope: dict[str, Any], attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kind = str(payload.get("type", "unknown"))
    base = {"timestamp": envelope.get("timestamp", ""), "raw_timestamp": envelope.get("timestamp", ""), "ordinal": envelope.get("ordinal"), "item_id": payload.get("id", ""), "source_type": kind}
    if kind == "commandExecution":
        call_id = str(payload.get("id") or "")
        command = str(payload.get("command") or "[命令未保存]")
        prefix = f"cwd: {payload.get('cwd') or '未找到'}\nexit_code: {payload.get('exitCode', '未保存')}\nstatus: {payload.get('status') or '未保存'}\n"
        output = payload.get("aggregatedOutput")
        return [
            {**base, "kind": "tool_call", "role": "tool", "name": "命令执行", "call_id": call_id, "text": command, "data": payload},
            {**base, "kind": "tool_result", "role": "tool", "name": "命令输出", "call_id": call_id, "text": prefix + (str(output) if output is not None else "[命令输出未保存]"), "data": payload},
        ]
    if kind in {"mcpToolCall", "dynamicToolCall"}:
        name = ".".join(str(payload.get(k)) for k in ("server", "tool") if payload.get(k)) or str(payload.get("tool") or kind)
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        arguments = _content_text(payload.get("arguments", payload.get("input", {})))
        call = {**base, "kind": "tool_call", "role": "tool", "name": name, "call_id": call_id, "text": arguments or "[参数未保存]", "data": payload}
        result = payload.get("result") or payload.get("contentItems")
        error = payload.get("error")
        result_text = _content_text(error if error else result) if error or result is not None else "[工具结果未保存]"
        output = {**base, "kind": "tool_result", "role": "tool", "name": name + " 结果", "call_id": call_id, "text": result_text, "data": payload}
        return [call, output]
    if kind == "functionCallOutput":
        return [{**base, "kind": "tool_result", "role": "tool", "name": str(payload.get("name") or "函数结果"), "call_id": str(payload.get("id") or ""), "text": str(payload.get("output") or "[工具结果未保存]"), "data": payload}]
    if kind == "subAgentActivity":
        return [{**base, "kind": "status", "role": "event", "name": "子代理活动", "text": _content_text(payload), "data": payload}]
    if kind == "imageView":
        path = payload.get("path")
        if path:
            attachments.append({"ref": str(path), "kind": "imageView"})
        return [{**base, "kind": "status", "role": "event", "name": "图片查看", "text": str(path or "[图片路径未保存]"), "data": payload}]
    record = _normalized_response(payload, envelope, attachments)
    return [record] if record else []


def _event_label(payload: dict[str, Any]) -> str:
    return str(payload.get("type", "unknown"))


def _iter_candidate_databases(root: str) -> list[str]:
    paths: list[str] = []
    for folder in (root, os.path.join(root, "sqlite")):
        if not os.path.isdir(folder):
            continue
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and entry.name.casefold().endswith((".sqlite", ".sqlite3", ".db")):
                    paths.append(entry.path)
        except OSError:
            continue
    return sorted(set(paths), key=str.casefold)


def _read_metadata(database: DatabaseSnapshot, index: dict[str, dict[str, Any]], by_id: dict[str, SessionInfo], issues: list[str]) -> None:
    if not database.snapshot_path:
        return
    try:
        connection = sqlite3.connect(f"file:{Path(database.snapshot_path).as_posix()}?mode=ro", uri=True, timeout=5)
        for schema in database.tables:
            if not schema.metadata_id:
                continue
            try:
                cursor = connection.execute(f"SELECT * FROM {_quote_identifier(schema.name)}")
                columns = [x[0] for x in cursor.description]
                for row in cursor:
                    data = dict(zip(columns, row))
                    sid = str(data.get(schema.metadata_id) or "").strip()
                    if not sid:
                        continue
                    item = by_id.setdefault(sid, SessionInfo(session_id=sid))
                    title_column = schema.title
                    title = data.get(title_column) if title_column else ""
                    if title and (not item.title or len(str(title)) < len(item.title)):
                        item.title = str(title)
                    lowered = {k.casefold(): v for k, v in data.items()}
                    cwd = next((lowered[k] for k in ("cwd", "working_directory", "workdir") if lowered.get(k)), "")
                    if cwd and not item.cwd:
                        item.cwd = str(cwd)
                    raw_created = next((lowered[k] for k in ("created_at", "created_at_ms", "source_created_at", "created_time", "created") if lowered.get(k) is not None), None)
                    raw_updated = next((lowered[k] for k in ("updated_at", "updated_at_ms", "source_updated_at", "recency_at", "last_updated") if lowered.get(k) is not None), None)
                    created = _epoch_string(raw_created)
                    updated = _epoch_string(raw_updated)
                    if created and (not item.raw_created_at or _time_sort_key(created) < _time_sort_key(item.raw_created_at)):
                        item.raw_created_at = created
                    if updated and _time_sort_key(updated) >= _time_sort_key(item.raw_updated_at):
                        item.raw_updated_at = updated
                    rollout = next((lowered[k] for k in ("rollout_path", "session_path", "path") if lowered.get(k)), "")
                    if rollout and str(rollout) not in item.paths:
                        item.paths.append(str(rollout))
                    archived = lowered.get("archived") or lowered.get("is_archived")
                    if archived not in (None, ""):
                        item.archived = bool(int(archived)) if str(archived).isdigit() else str(archived).casefold() in {"true", "yes", "archived"}
                    if sid in index:
                        idx = index[sid]
                        idx_title = idx.get("thread_name") or idx.get("title")
                        if idx_title and (not item.title or (len(item.title) > 180 and len(str(idx_title)) <= 180)):
                            item.title = str(idx_title)
            except sqlite3.Error as exc:
                issues.append(f"{database.source_path} / 表 {schema.name}：{type(exc).__name__}: {exc}")
        connection.close()
    except Exception as exc:
        issues.append(f"{database.source_path}：数据库元数据读取失败：{type(exc).__name__}: {exc}")


def _index_entries(root: str, issues: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    path = os.path.join(root, "session_index.jsonl")
    if not os.path.isfile(path):
        return result
    try:
        for line_no, line in _iter_jsonl(path):
            try:
                value = json.loads(line)
                sid = str(value.get("id") or value.get("session_id") or "")
                if sid:
                    result[sid] = value
            except Exception as exc:
                issues.append(f"session_index.jsonl 第 {line_no} 行损坏：{type(exc).__name__}")
    except OSError as exc:
        issues.append(f"session_index.jsonl 读取失败：{exc}")
    return result


def _inspect_history(root: str, notes: list[str], issues: list[str]) -> None:
    path = os.path.join(root, "history.jsonl")
    if not os.path.isfile(path):
        notes.append("history.jsonl：未找到；不影响以 rollout 为主的数据读取")
        return
    total = 0
    valid = 0
    bad = 0
    keys: set[str] = set()
    try:
        for line_no, line in _iter_jsonl(path):
            total += 1
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    valid += 1
                    keys.update(str(key) for key in value.keys())
                else:
                    bad += 1
            except Exception as exc:
                bad += 1
                issues.append(f"history.jsonl 第 {line_no} 行损坏：{type(exc).__name__}")
        notes.append(f"history.jsonl：检查 {total} 行，{valid} 行有效，{bad} 行损坏；字段 {', '.join(sorted(keys)) or '未识别'}。仅作为辅助索引，不作完整聊天记录。")
    except OSError as exc:
        issues.append(f"history.jsonl 读取失败：{type(exc).__name__}: {exc}")


def _scan_rollout_file(path: str, session: SessionInfo, cancel: threading.Event, progress: Callable[[str, int], None] | None = None, seen_ids: set[str] | None = None, seen_hashes: set[str] | None = None) -> None:
    seen_ids = seen_ids if seen_ids is not None else set()
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    counts = Counter(session.type_counts)
    search = session.search_text
    malformed_before = len(session.issues)
    meta_created = ""
    meta_cwd = ""
    try:
        for line_no, line in _iter_jsonl(path):
            if cancel.is_set():
                return
            try:
                entry = json.loads(line)
            except Exception as exc:
                session.issues.append(f"{os.path.basename(path)} 第 {line_no} 行 JSON 损坏：{type(exc).__name__}")
                continue
            if not isinstance(entry, dict):
                counts["<非对象 JSON>"] += 1
                continue
            record_type = str(entry.get("type", "<未知>") or "<未知>")
            payload = entry.get("payload")
            if record_type == "response_item" and isinstance(payload, dict) and payload.get("type") in {"message", "userMessage"}:
                if payload.get("type") == "userMessage" or payload.get("role") == "user":
                    session.attachment_ids.update(_codex_attachment_ids(payload.get("content", payload.get("text", ""))))
            elif record_type == "event_msg" and isinstance(payload, dict) and _event_label(payload) == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and (item.get("type") == "userMessage" or (item.get("type") == "message" and item.get("role") == "user")):
                    session.attachment_ids.update(_codex_attachment_ids(item.get("content", item.get("text", ""))))
            raw_timestamp = _epoch_string(entry.get("timestamp"))
            if raw_timestamp and _time_sort_key(raw_timestamp) >= _time_sort_key(session.raw_updated_at):
                session.raw_updated_at = raw_timestamp
            counts[record_type] += 1
            session.event_count += 1
            if record_type not in {"session_meta", "event_msg", "response_item", "turn_context", "token_usage_record", "world_state", *STATUS_RECORD_TYPES}:
                issue = f"{os.path.basename(path)} 第 {line_no} 行：未知记录类型 {record_type}"
                if issue not in session.issues:
                    session.issues.append(issue)
            if record_type == "session_meta" and isinstance(entry.get("payload"), dict):
                meta = entry["payload"]
                sid = meta.get("session_id") or meta.get("id")
                if sid and str(sid) != session.session_id:
                    session.issues.append(f"{os.path.basename(path)} 中会话 ID 与文件归属不一致：{sid}")
                meta_created = _epoch_string(meta.get("timestamp"))
                meta_cwd = str(meta.get("cwd") or "")
                continue
            payload = entry.get("payload")
            if record_type == "response_item" and isinstance(payload, dict):
                item_id = str(payload.get("id") or "")
                canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()
                if (item_id and item_id in seen_ids) or (not item_id and digest in seen_hashes):
                    session.duplicate_count += 1
                    continue
                if item_id:
                    seen_ids.add(item_id)
                else:
                    seen_hashes.add(digest)
                item_type = str(payload.get("type", "unknown"))
                if item_type == "message":
                    role = str(payload.get("role", ""))
                    phase = str(payload.get("phase", "")).casefold()
                    if phase not in {"analysis", "justify", "confidence", "delegate_summary", "juicepred"}:
                        attachments: list[dict[str, Any]] = []
                        text = _content_text(payload.get("content", ""), attachments)
                        if role == "user":
                            session.user_count += 1
                        elif role == "assistant":
                            session.assistant_count += 1
                        if text and len(search) < SEARCH_LIMIT:
                            search += "\n" + text[: SEARCH_LIMIT - len(search)]
                elif item_type == "reasoning":
                    continue
                elif item_type.endswith("_call") or item_type in {"custom_tool_call", "function_call"}:
                    session.tool_count += 1
                    for k in ("name", "input", "arguments"):
                        val = payload.get(k)
                        if isinstance(val, str) and len(search) < SEARCH_LIMIT:
                            search += "\n" + val[: SEARCH_LIMIT - len(search)]
                elif item_type.endswith("_output"):
                    val = _content_text(payload.get("output", payload.get("content", "")))
                    if val and len(search) < SEARCH_LIMIT:
                        search += "\n" + val[: SEARCH_LIMIT - len(search)]
            elif record_type == "event_msg" and isinstance(payload, dict):
                event_kind = _event_label(payload)
                if event_kind not in {"item_completed", "token_count", "task_started", "task_complete", "turn_aborted", "thread_settings_applied", "user_message", "error", "turn_abandoned", "agent_message_delta", "exec_command_begin", "exec_command_end"}:
                    issue = f"{os.path.basename(path)}：未知 event_msg 类型 {event_kind}"
                    if issue not in session.issues:
                        session.issues.append(issue)
                if event_kind not in {"item_completed", "token_count"}:
                    if event_kind not in {"task_started", "task_complete", "turn_aborted", "thread_settings_applied"}:
                        counts["event_msg:" + event_kind] += 0
                    for k in ("message", "error", "reason", "stderr", "stdout"):
                        val = payload.get(k)
                        if isinstance(val, str) and len(search) < SEARCH_LIMIT:
                            search += "\n" + val[: SEARCH_LIMIT - len(search)]
            if progress and line_no % 3000 == 0:
                progress(f"读取 {os.path.basename(path)}：{line_no:,} 行", -1)
    except Exception as exc:
        session.issues.append(f"{os.path.basename(path)} 读取失败：{type(exc).__name__}: {exc}")
    if meta_created and (not session.raw_created_at or _time_sort_key(meta_created) < _time_sort_key(session.raw_created_at)):
        session.raw_created_at = meta_created
    if meta_cwd and not session.cwd:
        session.cwd = meta_cwd
    if path not in session.paths:
        session.paths.append(path)
    session.type_counts = dict(counts)
    session.search_text = search[:SEARCH_LIMIT]
    session.archived = session.archived or os.path.basename(os.path.dirname(path)).casefold() == "archived_sessions"
    if len(session.issues) > malformed_before:
        session.status = "部分可读，有损坏行"


def _search_database_fallback(session: SessionInfo, databases: list[DatabaseSnapshot], cancel: threading.Event, issues: list[str]) -> None:
    if any(os.path.isfile(path) for path in session.paths):
        return
    seen: set[str] = set()
    for database in databases:
        if cancel.is_set() or not database.snapshot_path:
            continue
        for schema in database.tables:
            if not schema.item_json:
                continue
            lowered = {c.casefold(): c for c in schema.columns}
            thread_col = lowered.get("thread_id") or lowered.get("session_id")
            if not thread_col:
                continue
            try:
                conn = sqlite3.connect(f"file:{Path(database.snapshot_path).as_posix()}?mode=ro", uri=True, timeout=5)
                sql = f"SELECT * FROM {_quote_identifier(schema.name)} WHERE {_quote_identifier(thread_col)} = ?"
                cursor = conn.execute(sql, (session.session_id,))
                columns = [x[0] for x in cursor.description]
                rows = cursor.fetchall()
                if not rows:
                    conn.close()
                    continue
                json_column = schema.item_json
                search = session.search_text
                for row in rows:
                    data = dict(zip(columns, row))
                    raw = data.get(json_column)
                    try:
                        item = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        session.issues.append(f"{os.path.basename(database.source_path)} / {schema.name}：資料庫 JSON 條目損壞")
                        continue
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id") or data.get(lowered.get("item_id", "")) or "")
                    digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
                    if item_id and item_id in seen or not item_id and digest in seen:
                        session.duplicate_count += 1
                        continue
                    seen.add(item_id or digest)
                    kind = str(item.get("type") or data.get(schema.item_type, "unknown"))
                    session.type_counts[kind] = session.type_counts.get(kind, 0) + 1
                    session.event_count += 1
                    if kind in {"userMessage", "user_message"}:
                        session.user_count += 1
                        text = _content_text(item.get("content", item.get("text", "")))
                    elif kind in {"agentMessage", "assistantMessage", "assistant_message"}:
                        session.assistant_count += 1
                        text = str(item.get("text") or _content_text(item.get("content", "")))
                    elif kind.casefold() in {"reasoning", "thought"}:
                        continue
                    else:
                        text = _content_text(item)
                        if "tool" in kind.casefold() or "command" in kind.casefold():
                            session.tool_count += 1
                    if text and len(search) < SEARCH_LIMIT:
                        session.search_text = (search + "\n" + text)[:SEARCH_LIMIT]
                        search = session.search_text
                conn.close()
                if rows:
                    session.paths.append(database.source_path + " :: " + schema.name)
                    session.source = "SQLite 条目回退"
            except Exception as exc:
                issues.append(f"{database.source_path} / {schema.name}：回退读取失败：{type(exc).__name__}: {exc}")


def _filename_id(path: str) -> str:
    name = os.path.basename(path)
    stem = name.removesuffix(".jsonl.zst").removesuffix(".jsonl")
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})", stem)
    if match:
        return match.group(1).rstrip("_")
    parts = re.split(r"[_-]", stem)
    return parts[-1] if parts else stem


def scan_codex_home(root: str, cancel: threading.Event | None = None, progress: Callable[[str, int], None] | None = None) -> Catalog:
    cancel = cancel or threading.Event()
    root = os.path.abspath(os.path.expandvars(root))
    catalog = Catalog(root=root, temp_dir=tempfile.mkdtemp(prefix="codex-history-viewer-"))
    by_id: dict[str, SessionInfo] = {}
    index = _index_entries(root, catalog.issues)
    _inspect_history(root, catalog.notes, catalog.issues)
    for sid, entry in index.items():
        session = by_id.setdefault(sid, SessionInfo(session_id=sid))
        title = entry.get("thread_name") or entry.get("title")
        if title:
            session.title = str(title)
        updated = _epoch_string(entry.get("updated_at"))
        if updated:
            session.raw_updated_at = updated

    database_paths = _iter_candidate_databases(root)
    for i, path in enumerate(database_paths):
        if cancel.is_set():
            catalog.issues.append("資料庫掃描已取消")
            break
        database = _open_database_snapshot(path, catalog.temp_dir)
        catalog.databases.append(database)
        if database.error:
            catalog.issues.append(f"{path}：{database.error}")
        elif progress:
            recognized = [t.name for t in database.tables if t.metadata_id or t.item_json]
            progress(f"已檢查資料庫 {i + 1}/{len(database_paths)}：{os.path.basename(path)}（{', '.join(recognized) or '無相關表'}）", -1)
        _read_metadata(database, index, by_id, catalog.issues)

    rollout_paths: list[str] = []
    for folder in (os.path.join(root, "sessions"), os.path.join(root, "archived_sessions")):
        if os.path.isdir(folder):
            for dirpath, _, filenames in os.walk(folder):
                if cancel.is_set():
                    break
                for filename in filenames:
                    lowered = filename.casefold()
                    if lowered.startswith("rollout-") and (lowered.endswith(".jsonl") or lowered.endswith(".jsonl.zst")):
                        rollout_paths.append(os.path.join(dirpath, filename))
    rollout_paths.sort(key=str.casefold)
    seen_response_ids: dict[str, set[str]] = {}
    seen_response_hashes: dict[str, set[str]] = {}
    total_bytes = sum(os.path.getsize(p) for p in rollout_paths if os.path.isfile(p))
    done_bytes = 0
    for index_path, path in enumerate(rollout_paths):
        if cancel.is_set():
            catalog.issues.append("會話掃描已取消；已完成的項目仍可查看")
            break
        sid = _filename_id(path)
        try:
            first = None
            for _, line in _iter_jsonl(path):
                if not line.strip():
                    continue
                candidate = json.loads(line)
                if isinstance(candidate, dict) and candidate.get("type") == "session_meta":
                    first = candidate
                    break
            if isinstance(first, dict):
                payload = first.get("payload", {})
                sid = str(payload.get("session_id") or payload.get("id") or sid)
        except StopIteration:
            pass
        except Exception as exc:
            catalog.issues.append(f"{path}：無法读取首条元数据：{type(exc).__name__}: {exc}")
        session = by_id.setdefault(sid, SessionInfo(session_id=sid))
        if path.casefold().endswith(".zst"):
            session.archived = True
        if path not in session.paths:
            session.paths.append(path)
        if not session.source:
            session.source = "归档 JSONL.ZST" if path.casefold().endswith(".zst") else "rollout JSONL"
        try:
            _scan_rollout_file(
                path,
                session,
                cancel,
                progress,
                seen_response_ids.setdefault(sid, set()),
                seen_response_hashes.setdefault(sid, set()),
            )
        except Exception as exc:
            session.issues.append(f"{path}：{type(exc).__name__}: {exc}")
            session.status = "读取失败"
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        done_bytes += size
        catalog.files_scanned += 1
        catalog.bytes_scanned += size
        if progress:
            pct = int(done_bytes * 100 / total_bytes) if total_bytes else 100
            progress(f"扫描会话 {index_path + 1}/{len(rollout_paths)}：{os.path.basename(path)}", pct)

    for session in by_id.values():
        _search_database_fallback(session, catalog.databases, cancel, catalog.issues)
        if not session.title or len(session.title) > 180 or "\n" in session.title:
            candidate = next((part.strip() for part in session.search_text.splitlines() if part.strip()), "")
            session.title = candidate[:100] if candidate else session.title[:100]
        if not session.title:
            session.title = "未命名会话"
        if not session.raw_created_at and session.paths:
            session.raw_created_at = ""
        if not session.raw_updated_at:
            session.raw_updated_at = session.raw_created_at
        session.created_at = _local_display(session.raw_created_at)
        session.updated_at = _local_display(session.raw_updated_at)
        own_attachment_folder = Path(root) / "attachments" / session.session_id
        linked_attachment_folder = any(
            (folder := Path(root) / "attachments" / item).is_dir() and not folder.is_symlink()
            for item in session.attachment_ids
        )
        session.has_attachment = own_attachment_folder.is_dir() or linked_attachment_folder
        if session.issues and session.status == "未扫描":
            session.status = "部分可读"
        elif session.paths and session.status == "未扫描":
            session.status = "可读取"
        elif session.source == "SQLite 条目回退":
            session.status = "仅数据库条目"
        elif not any(os.path.isfile(path) for path in session.paths):
            session.status = "未找到正文"
            if not session.issues:
                session.issues.append("未找到 rollout 正文；已检查可识别的 SQLite 条目回退来源")
    catalog.sessions = sorted(by_id.values(), key=lambda s: (_time_sort_key(s.raw_updated_at), s.session_id), reverse=True)
    return catalog


def _event_time(entry: dict[str, Any]) -> tuple[float, int, int]:
    raw = str(entry.get("timestamp", ""))
    ordinal = entry.get("ordinal")
    try:
        ordinal_value = int(ordinal)
    except (TypeError, ValueError):
        ordinal_value = 0
    return (_time_sort_key(raw), ordinal_value, int(entry.get("_sequence", 0)))


def _attachment_exists(reference: Any, session: SessionInfo, catalog: Catalog, related_names: set[str]) -> bool:
    if not isinstance(reference, str) or not reference:
        return False
    if reference.startswith("data:image/") and ";base64," in reference:
        return True
    value = reference
    if value.casefold().startswith("file://"):
        value = value[7:]
        if value.startswith("/") and len(value) > 2 and value[2] == ":":
            value = value[1:]
    if os.path.isfile(value):
        return True
    basename = os.path.basename(value.replace("\\", "/")).casefold()
    if basename and basename in related_names:
        return True
    if os.path.isdir(os.path.join(catalog.root, "attachments", session.session_id)) and basename:
        return False
    return False


def _load_portable_events(session: SessionInfo) -> tuple[list[dict[str, Any]], list[str], Counter[str]]:
    issues = list(session.issues)
    type_counts: Counter[str] = Counter()
    if not session.paths:
        return [], issues, type_counts
    try:
        portable_path = Path(session.paths[0])
        payload = json.loads(portable_path.read_text(encoding="utf-8"))
        source_events = payload.get("events", [])
        if not isinstance(source_events, list):
            raise ValueError("导入包中的 events 格式无效")
        root = portable_path.parent.resolve()
        events: list[dict[str, Any]] = []
        for source in source_events:
            if not isinstance(source, dict):
                continue
            event = dict(source)
            kind = str(event.get("kind") or "unknown")
            type_counts[str(event.get("source_type") or kind)] += 1
            attachments: list[dict[str, Any]] = []
            for raw_attachment in event.get("attachments", []):
                if not isinstance(raw_attachment, dict):
                    continue
                attachment = dict(raw_attachment)
                relative = attachment.get("path")
                if isinstance(relative, str) and relative:
                    candidate = (root / Path(*Path(relative.replace("\\", "/")).parts)).resolve()
                    try:
                        inside_package = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate)))) == os.path.normcase(str(root))
                    except (OSError, ValueError):
                        inside_package = False
                    if inside_package and candidate.is_file():
                        attachment["ref"] = str(candidate)
                        attachment["path"] = str(candidate)
                        attachment["status"] = "本机文件存在"
                    else:
                        attachment["ref"] = str(attachment.get("reference") or relative)
                        attachment["path"] = ""
                        attachment["status"] = "导入包中未找到"
                        issue = f"导入包附件未找到或路径无效：{relative}"
                        if issue not in issues:
                            issues.append(issue)
                else:
                    attachment.setdefault("ref", str(attachment.get("reference") or ""))
                attachments.append(attachment)
            event["attachments"] = attachments
            events.append(event)
        return events, issues, type_counts
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        issues.append(f"导入会话读取失败：{type(exc).__name__}: {exc}")
        return [], issues, type_counts


def load_session_events(session: SessionInfo, catalog: Catalog, cancel: threading.Event | None = None) -> tuple[list[dict[str, Any]], list[str], Counter[str]]:
    cancel = cancel or threading.Event()
    if session.source == "导入 ZIP":
        return _load_portable_events(session)
    issues: list[str] = list(session.issues)
    type_counts: Counter[str] = Counter()
    normalized: list[dict[str, Any]] = []
    canonical_ids: set[str] = set()
    canonical_hashes: set[str] = set()
    item_completed: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    sequence = 0
    source_paths = [p for p in session.paths if os.path.isfile(p)]
    for path in source_paths:
        if cancel.is_set():
            break
        try:
            for line_no, line in _iter_jsonl(path):
                if cancel.is_set():
                    break
                try:
                    entry = json.loads(line)
                except Exception as exc:
                    issues.append(f"{os.path.basename(path)} 第 {line_no} 行 JSON 损坏：{type(exc).__name__}")
                    continue
                if not isinstance(entry, dict):
                    continue
                record_type = str(entry.get("type", "unknown"))
                type_counts[record_type] += 1
                payload = entry.get("payload")
                if record_type == "session_meta":
                    continue
                sequence += 1
                event_base = {"timestamp": entry.get("timestamp", ""), "raw_timestamp": entry.get("timestamp", ""), "ordinal": entry.get("ordinal"), "_sequence": sequence, "source_type": record_type}
                if record_type == "response_item" and isinstance(payload, dict):
                    item_id = str(payload.get("id") or "")
                    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
                    if item_id and item_id in canonical_ids or not item_id and digest in canonical_hashes:
                        continue
                    if item_id:
                        canonical_ids.add(item_id)
                    else:
                        canonical_hashes.add(digest)
                    attachments: list[dict[str, Any]] = []
                    record = _normalized_response(payload, entry, attachments)
                    if record:
                        if payload.get("type") in {"message", "userMessage"} and (payload.get("type") == "userMessage" or payload.get("role") == "user"):
                            _append_codex_attachment_refs(payload.get("content", payload.get("text", "")), catalog.root, attachments, session)
                        record["attachments"] = attachments
                        record["_sequence"] = sequence
                        normalized.append(record)
                elif record_type == "event_msg" and isinstance(payload, dict):
                    event_kind = _event_label(payload)
                    if event_kind == "item_completed" and isinstance(payload.get("item"), dict):
                        item_completed.append((payload, event_base, sequence))
                    elif event_kind not in {"token_count", "turn_context", "session_meta"}:
                        normalized.append({**event_base, "kind": "status", "role": "event", "name": event_kind, "text": _content_text(payload), "data": payload})
                else:
                    if record_type not in {"turn_context", "token_usage_record", "world_state"}:
                        kind = "status" if record_type in STATUS_RECORD_TYPES else "unknown"
                        normalized.append({**event_base, "kind": kind, "role": "event", "name": record_type, "text": _content_text(payload), "data": payload})
        except Exception as exc:
            issues.append(f"{os.path.basename(path)} 读取失败：{type(exc).__name__}: {exc}")

    for payload, event_base, order in item_completed:
        item = payload.get("item")
        item_id = str(item.get("id") or "")
        if item_id and item_id in canonical_ids:
            continue
        digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
        if not item_id and digest in canonical_hashes:
            continue
        if item_id:
            canonical_ids.add(item_id)
        else:
            canonical_hashes.add(digest)
        attachments: list[dict[str, Any]] = []
        record = _normalized_response(item, event_base, attachments)
        if record:
            if item.get("type") in {"message", "userMessage"} and (item.get("type") == "userMessage" or item.get("role") == "user"):
                _append_codex_attachment_refs(item.get("content", item.get("text", "")), catalog.root, attachments, session)
            record["_sequence"] = order
            record["attachments"] = attachments
            normalized.append(record)

    if not source_paths:
        for database in catalog.databases:
            if not database.snapshot_path:
                continue
            for schema in database.tables:
                if not schema.item_json:
                    continue
                lowered = {c.casefold(): c for c in schema.columns}
                thread_col = lowered.get("thread_id") or lowered.get("session_id")
                if not thread_col:
                    continue
                try:
                    connection = sqlite3.connect(f"file:{Path(database.snapshot_path).as_posix()}?mode=ro", uri=True, timeout=5)
                    order_col = lowered.get("rollout_ordinal") or lowered.get("ordinal") or lowered.get("created_at_ms")
                    order_sql = f" ORDER BY {_quote_identifier(order_col)}" if order_col else ""
                    cursor = connection.execute(f"SELECT * FROM {_quote_identifier(schema.name)} WHERE {_quote_identifier(thread_col)} = ?{order_sql}", (session.session_id,))
                    columns = [x[0] for x in cursor.description]
                    for row in cursor:
                        if cancel.is_set():
                            break
                        data = dict(zip(columns, row))
                        try:
                            item = json.loads(data.get(schema.item_json) or "{}")
                        except Exception as exc:
                            issues.append(f"数据库条目 JSON 损坏：{type(exc).__name__}")
                            continue
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("id") or data.get(lowered.get("item_id", "")) or "")
                        digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
                        if item_id and item_id in canonical_ids or not item_id and digest in canonical_hashes:
                            continue
                        if item_id:
                            canonical_ids.add(item_id)
                        else:
                            canonical_hashes.add(digest)
                        timestamp = _epoch_string(data.get(lowered.get("created_at_ms", "")) or data.get(lowered.get("completed_at_ms", "")))
                        envelope = {"timestamp": timestamp, "ordinal": data.get(lowered.get("rollout_ordinal", ""))}
                        attachments: list[dict[str, Any]] = []
                        records = _normalized_database_items(item, envelope, attachments)
                        if item.get("type") in {"message", "userMessage"} and (item.get("type") == "userMessage" or item.get("role") == "user"):
                            _append_codex_attachment_refs(item.get("content", item.get("text", "")), catalog.root, attachments, session)
                        for record in records:
                            record["attachments"] = attachments
                            record["_sequence"] = data.get(lowered.get("rollout_ordinal", "")) or sequence + 1
                            normalized.append(record)
                        if timestamp and _time_sort_key(timestamp) >= _time_sort_key(session.raw_updated_at):
                            session.raw_updated_at = timestamp
                        type_counts[str(item.get("type", "unknown"))] += 1
                    connection.close()
                except Exception as exc:
                    issues.append(f"{database.source_path} / {schema.name}：条目读取失败：{type(exc).__name__}: {exc}")
    related_folder = os.path.join(catalog.root, "attachments", session.session_id)
    related_names: set[str] = set()
    if os.path.isdir(related_folder):
        try:
            for directory, _, filenames in os.walk(related_folder):
                for filename in filenames:
                    related_names.add(filename.casefold())
        except OSError as exc:
            issues.append(f"附件目录读取失败：{type(exc).__name__}: {exc}")
    for event in normalized:
        for attachment in event.get("attachments", []):
            reference = attachment.get("ref")
            exists = _attachment_exists(reference, session, catalog, related_names)
            attachment["status"] = "本地文件存在" if exists else "未找到或仅有外部引用"
            if not exists:
                issue = f"附件未找到或不可离线读取：{str(reference)[:180]}"
                if issue not in issues:
                    issues.append(issue)
    normalized.sort(key=_event_time)
    return normalized, issues, type_counts

