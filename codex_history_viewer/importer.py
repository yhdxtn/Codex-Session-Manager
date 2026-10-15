from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .reader import APP_VERSION, SessionInfo


_MAX_METADATA_BYTES = 256 * 1024 * 1024
_MAX_BUNDLE_SESSIONS = 100_000
_SEARCH_LIMIT = 256_000
_WINDOWS_RESERVED_NAME = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$")


class DuplicateSessionError(Exception):
    pass


class ImportCancelled(Exception):
    pass


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return ""
    for part in raw_parts:
        if (
            part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or _WINDOWS_RESERVED_NAME.fullmatch(part)
        ):
            return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", normalized):
        return ""
    return path.as_posix()


def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name in members:
            raise ValueError(f"ZIP 中存在重名文件：{name}")
        members[name] = info
    return members


def _listed_hash(manifest: dict[str, Any], relative_path: str) -> tuple[str, int] | None:
    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        return None
    for item in entries:
        if isinstance(item, dict) and item.get("path") == relative_path:
            try:
                return str(item["sha256"]), int(item["size_bytes"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _verify_metadata(data: bytes, expected: tuple[str, int] | None, label: str) -> None:
    if expected is None:
        return
    digest, size = expected
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"{label} 与 manifest.json 中记录的哈希不一致")


def _copy_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    expected: tuple[str, int] | None,
    cancel=None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as source, destination.open("wb") as target:
            while True:
                if cancel is not None and cancel.is_set():
                    raise ImportCancelled()
                block = source.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
                digest.update(block)
                size += len(block)
        if expected is not None and (size != expected[1] or digest.hexdigest() != expected[0]):
            raise ValueError(f"附件 {destination.name} 与 manifest.json 中记录的哈希不一致")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _package_data(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    candidates = [name for name in members if PurePosixPath(name).name == "聊天记录.json"]
    if len(candidates) != 1:
        raise ValueError("ZIP 必须且只能包含一个聊天记录.json")
    json_name = candidates[0]
    prefix = str(PurePosixPath(json_name).parent)
    if prefix == ".":
        prefix = ""
    manifest_name = f"{prefix}/manifest.json" if prefix else "manifest.json"
    manifest: dict[str, Any] = {}
    manifest_info = members.get(manifest_name)
    if manifest_info:
        if manifest_info.file_size > _MAX_METADATA_BYTES:
            raise ValueError("manifest.json 过大")
        try:
            manifest = json.loads(archive.read(manifest_info))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("manifest.json 无法读取") from exc
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 格式无效")

    json_info = members[json_name]
    if json_info.file_size > _MAX_METADATA_BYTES:
        raise ValueError("聊天记录.json 过大")
    raw = archive.read(json_info)
    relative_json = "聊天记录.json"
    _verify_metadata(raw, _listed_hash(manifest, relative_json), relative_json)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("聊天记录.json 无法解析") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict) or not isinstance(payload.get("events"), list):
        raise ValueError("该 ZIP 不是本工具生成的可导入会话包")
    for index, event in enumerate(payload["events"], 1):
        if not isinstance(event, dict):
            raise ValueError(f"聊天记录.json 第 {index} 条事件格式无效")
        attachments = event.get("attachments", [])
        if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
            raise ValueError(f"聊天记录.json 第 {index} 条事件的附件列表格式无效")
    return prefix, relative_json, payload, manifest


def _event_attachment_paths(payload: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        attachments = event.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            relative = _safe_relative_path(attachment.get("path"))
            if relative:
                paths.add(relative)
    return paths


def _import_package_archive(
    archive: zipfile.ZipFile,
    import_root: str,
    existing_session_ids: set[str],
    cancel=None,
    expected_session_id: str = "",
) -> SessionInfo:
    members = _archive_members(archive)
    prefix, _, payload, manifest = _package_data(archive, members)
    source_data = payload["session"]
    session_id = str(source_data.get("session_id") or "")
    if not session_id:
        raise ValueError("会话包缺少会话 ID")
    if expected_session_id and session_id != expected_session_id:
        raise ValueError("迁移清单中的会话 ID 与会话包不一致")
    if session_id in existing_session_ids:
        raise DuplicateSessionError(str(source_data.get("title") or session_id))
    events = payload["events"]
    for event in events:
        for attachment in event.get("attachments", []):
            raw_path = attachment.get("path")
            if raw_path and not _safe_relative_path(raw_path):
                raise ValueError(f"迁移包包含不安全的附件路径：{raw_path}")
    expected_paths = _event_attachment_paths(payload)
    windows_paths: dict[str, str] = {}
    for relative in expected_paths:
        safe_relative = _safe_relative_path(relative)
        if not safe_relative:
            raise ValueError(f"迁移包包含不安全的附件路径：{relative}")
        folded = safe_relative.casefold()
        previous = windows_paths.get(folded)
        if previous is not None and previous != safe_relative:
            raise ValueError(f"迁移包包含在 Windows 上重名的附件：{previous} / {safe_relative}")
        windows_paths[folded] = safe_relative

    target = Path(import_root) / uuid.uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    issues: list[str] = []
    try:
        for relative in sorted(expected_paths):
            if cancel is not None and cancel.is_set():
                raise ImportCancelled()
            if not relative.startswith("attachments/"):
                issues.append(f"附件路径不受支持：{relative}")
                continue
            member_name = f"{prefix}/{relative}" if prefix else relative
            info = members.get(member_name)
            if info is None:
                issues.append(f"导入包缺少附件：{relative}")
                continue
            destination = target / Path(*PurePosixPath(relative).parts)
            expected = _listed_hash(manifest, relative)
            try:
                _copy_member(archive, info, destination, expected, cancel)
            except ImportCancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(f"附件导入失败：{relative}：{exc}")

        if cancel is not None and cancel.is_set():
            raise ImportCancelled()
        portable_path = target / "portable-session.json"
        portable_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    search_parts: list[str] = []
    type_counts: Counter[str] = Counter()
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("text"):
            search_parts.append(str(event["text"]))
        if event.get("name"):
            search_parts.append(str(event["name"]))
        source_type = str(event.get("source_type") or event.get("kind") or "unknown")
        type_counts[source_type] += 1
    search_text = "\n".join(search_parts)[:_SEARCH_LIMIT]
    for inherited in (source_data.get("issues", []), payload.get("read_issues", [])):
        if isinstance(inherited, list):
            issues.extend(str(item) for item in inherited if item and str(item) not in issues)

    return SessionInfo(
        session_id=session_id,
        title=str(source_data.get("title") or "未命名会话"),
        created_at=str(source_data.get("created_at") or ""),
        updated_at=str(source_data.get("updated_at") or ""),
        raw_created_at=str(source_data.get("raw_created_at") or ""),
        raw_updated_at=str(source_data.get("raw_updated_at") or ""),
        cwd=str(source_data.get("cwd") or ""),
        archived=bool(source_data.get("archived", False)),
        user_count=_int_value(source_data.get("user_count")),
        assistant_count=_int_value(source_data.get("assistant_count")),
        tool_count=_int_value(source_data.get("tool_count")),
        event_count=len(events),
        source="导入 ZIP",
        status="已导入",
        paths=[str(portable_path)],
        search_text=search_text,
        type_counts=dict(type_counts),
        issues=issues,
        duplicate_count=_int_value(source_data.get("duplicate_count")),
        has_attachment=bool(expected_paths),
    )


def _import_one(archive_path: str, import_root: str, existing_session_ids: set[str], cancel=None) -> SessionInfo:
    with zipfile.ZipFile(archive_path, "r") as archive:
        return _import_package_archive(archive, import_root, existing_session_ids, cancel)


def _read_bundle_manifest(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]) -> list[dict[str, Any]]:
    info = members.get("bundle_manifest.json")
    if info is None:
        raise ValueError("迁移包缺少 bundle_manifest.json")
    if info.file_size > _MAX_METADATA_BYTES:
        raise ValueError("迁移包清单过大")
    try:
        manifest = json.loads(archive.read(info))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("迁移包清单无法读取") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "codex-history-viewer-migration":
        raise ValueError("该 ZIP 不是本工具生成的整库迁移包")
    if manifest.get("version") != 1:
        raise ValueError("迁移包版本不受支持")
    entries = manifest.get("sessions")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_BUNDLE_SESSIONS:
        raise ValueError("迁移包会话清单无效")
    if manifest.get("session_count") != len(entries):
        raise ValueError("迁移包会话数量与清单不一致")

    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("迁移包包含无效的会话条目")
        relative = _safe_relative_path(entry.get("archive"))
        parts = PurePosixPath(relative).parts
        session_id = entry.get("session_id")
        digest = entry.get("sha256")
        try:
            size = int(entry.get("size_bytes"))
        except (TypeError, ValueError):
            size = -1
        if (
            not relative
            or len(parts) != 2
            or parts[0] != "sessions"
            or not parts[1].casefold().endswith(".zip")
            or relative in paths
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
            or size < 0
        ):
            raise ValueError("迁移包包含无效的会话条目")
        paths.add(relative)
        member = members.get(relative)
        if member is None or member.is_dir() or member.file_size != size:
            raise ValueError(f"迁移包缺少会话包或大小不一致：{relative}")
    return entries


def _verify_bundle_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    expected_digest: str,
    expected_size: int,
    cancel=None,
) -> None:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as source:
        while True:
            if cancel is not None and cancel.is_set():
                raise ImportCancelled()
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    if size != expected_size or digest.hexdigest().casefold() != expected_digest.casefold():
        raise ValueError("会话包与迁移清单中的 SHA-256 不一致")


def _import_bundle(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    import_root: str,
    known_ids: set[str],
    cancel,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    entries = _read_bundle_manifest(archive, members)
    results: list[dict[str, str]] = []
    imported = 0
    skipped = 0
    failed = 0

    for index, entry in enumerate(entries, 1):
        if cancel.is_set():
            break
        relative = entry["archive"]
        title = str(entry.get("title") or entry["session_id"])
        try:
            info = members[relative]
            _verify_bundle_member(archive, info, entry["sha256"], int(entry["size_bytes"]), cancel)
            if cancel.is_set():
                break
            with archive.open(info, "r") as session_stream:
                with zipfile.ZipFile(session_stream, "r") as session_archive:
                    session = _import_package_archive(
                        session_archive,
                        import_root,
                        known_ids,
                        cancel,
                        str(entry["session_id"]),
                    )
            imported += 1
            known_ids.add(session.session_id)
            results.append(
                {"status": "已导入", "path": relative, "title": session.title, "message": session.session_id}
            )
        except DuplicateSessionError as exc:
            skipped += 1
            results.append({"status": "已跳过", "path": relative, "title": str(exc), "message": "本机已存在相同会话 ID"})
        except ImportCancelled:
            break
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, KeyError) as exc:
            failed += 1
            results.append({"status": "失败", "path": relative, "title": title, "message": f"{type(exc).__name__}: {exc}"})
        if progress:
            progress(f"正在导入迁移包 {index}/{len(entries)}：{title[:60]}", int(index * 100 / len(entries)))

    return {
        "results": results,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancel.is_set(),
    }


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _issues_from_payload(data: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    issues = []
    for items in (data.get("issues", []), payload.get("read_issues", [])):
        if isinstance(items, list):
            issues.extend(str(item) for item in items if item and str(item) not in issues)
    return issues


def load_imported_sessions(import_root: str) -> list[SessionInfo]:
    root = Path(import_root)
    if not root.is_dir():
        return []
    sessions = []
    for folder in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        portable_path = folder / "portable-session.json"
        if not folder.is_dir() or not portable_path.is_file():
            continue
        try:
            payload = json.loads(portable_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict) or not isinstance(payload.get("events"), list):
                continue
            data = payload["session"]
            session_id = str(data.get("session_id") or "")
            if not session_id:
                continue
            events = payload["events"]
            parts = [
                str(value)
                for event in events
                if isinstance(event, dict)
                for value in (event.get("text"), event.get("name"))
                if value
            ]
            inherited_issues = _issues_from_payload(data, payload)
            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    title=str(data.get("title") or "未命名会话"),
                    created_at=str(data.get("created_at") or ""),
                    updated_at=str(data.get("updated_at") or ""),
                    raw_created_at=str(data.get("raw_created_at") or ""),
                    raw_updated_at=str(data.get("raw_updated_at") or ""),
                    cwd=str(data.get("cwd") or ""),
                    archived=bool(data.get("archived", False)),
                    user_count=_int_value(data.get("user_count")),
                    assistant_count=_int_value(data.get("assistant_count")),
                    tool_count=_int_value(data.get("tool_count")),
                    event_count=len(events),
                    source="导入 ZIP",
                    status="已导入",
                    paths=[str(portable_path)],
                    search_text="\n".join(parts)[:_SEARCH_LIMIT],
                    type_counts=dict(Counter(str(event.get("source_type") or event.get("kind") or "unknown") for event in events if isinstance(event, dict))),
                    issues=inherited_issues,
                    duplicate_count=_int_value(data.get("duplicate_count")),
                    has_attachment=any(
                        isinstance(attachment, dict) and bool(attachment.get("path"))
                        for event in events if isinstance(event, dict)
                        for attachment in event.get("attachments", [])
                    ),
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return sessions


def import_archives(
    archive_paths: list[str],
    import_root: str,
    existing_session_ids: set[str] | None = None,
    cancel=None,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    import threading

    cancel = cancel or threading.Event()
    os.makedirs(import_root, exist_ok=True)
    known_ids = set(existing_session_ids or ())
    results: list[dict[str, str]] = []
    imported = 0
    skipped = 0
    failed = 0
    total = max(1, len(archive_paths))
    for index, archive_path in enumerate(archive_paths, 1):
        if cancel.is_set():
            break
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = _archive_members(archive)
                if "bundle_manifest.json" in members:
                    bundle_result = _import_bundle(
                        archive,
                        members,
                        import_root,
                        known_ids,
                        cancel,
                        (
                            lambda text, value: progress(
                                text,
                                int(((index - 1) + value / 100) * 100 / total),
                            )
                        )
                        if progress
                        else None,
                    )
                    imported += int(bundle_result["imported"])
                    skipped += int(bundle_result["skipped"])
                    failed += int(bundle_result["failed"])
                    results.extend(
                        {
                            **item,
                            "path": f"{archive_path} :: {item.get('path', '')}",
                        }
                        for item in bundle_result["results"]
                    )
                else:
                    session = _import_package_archive(archive, import_root, known_ids, cancel)
                    imported += 1
                    known_ids.add(session.session_id)
                    results.append({"status": "已导入", "path": archive_path, "title": session.title, "message": session.session_id})
        except DuplicateSessionError as exc:
            skipped += 1
            results.append({"status": "已跳过", "path": archive_path, "title": str(exc), "message": "本机已存在相同会话 ID"})
        except ImportCancelled:
            results.append({"status": "已取消", "path": archive_path, "title": Path(archive_path).name, "message": "已取消当前导入"})
            break
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, KeyError) as exc:
            failed += 1
            results.append({"status": "失败", "path": archive_path, "title": Path(archive_path).name, "message": f"{type(exc).__name__}: {exc}"})
        if progress and not cancel.is_set():
            progress(f"导入包 {index}/{len(archive_paths)}：{Path(archive_path).name}", int(index * 100 / total))
    return {
        "results": results,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancel.is_set(),
        "sessions": load_imported_sessions(import_root),
        "tool_version": APP_VERSION,
    }
