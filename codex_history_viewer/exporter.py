from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from .reader import APP_VERSION, Catalog, SessionInfo, load_session_events


_SECRET_VALUE = re.compile(
    r"(?i)((?:\b(?:password|passwd|token|api[_-]?key|secret|authorization|access[_-]?key)\b|密码|口令|令牌|密钥)\s*[:=]\s*)(['\"]?)([^\s'\"&,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_ATTACHMENT_MARKER = re.compile(r"\[\[ATTACHMENT:(.*?)\]\]", re.S)
_SECRET_KEY_NAME = re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret|authorization|credential|access[_-]?key|密码|口令|令牌|密钥)")


def safe_name(value: str, limit: int = 70) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    value = value[:limit].rstrip(" ._")
    if not value:
        value = "未命名会话"
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def unique_directory(parent: str, base: str) -> str:
    candidate = os.path.join(parent, base)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, f"{base}_{suffix}")
        suffix += 1
    os.makedirs(candidate)
    return candidate


def redact_text(text: str) -> tuple[str, int]:
    count = 0

    def secret_repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group(1) + match.group(2) + "[已脱敏: 敏感值]"

    text = _SECRET_VALUE.sub(secret_repl, text)
    text, n = _BEARER.subn("Bearer [已脱敏: 令牌]", text)
    count += n
    text, n = _PRIVATE_KEY.subn("[已脱敏: 私钥]", text)
    count += n
    return text, count


def _sanitize(value: Any, redact: bool, counter: list[int]) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in {"encrypted_content", "reasoning", "analysis"}:
                continue
            if redact and _SECRET_KEY_NAME.search(str(key)):
                result[key] = "[已脱敏: 敏感字段]"
            else:
                result[key] = _sanitize(child, redact, counter)
        return result
    if isinstance(value, list):
        return [_sanitize(item, redact, counter) for item in value]
    if isinstance(value, str) and redact:
        cleaned, count = redact_text(value)
        counter[0] += count
        return cleaned
    return value


def _copy_related_attachments(session: SessionInfo, catalog: Catalog, target: str, events: list[dict[str, Any]], cancel, issues: list[str], redact: bool = False, redaction_counter: list[int] | None = None) -> dict[str, str]:
    """Copy only the session-ID attachment folder and exact paths referenced by its records."""
    root = Path(catalog.root)
    attachment_root = root / "attachments"
    session_folder = attachment_root / session.session_id
    output_root = Path(target)
    copied: dict[str, str] = {}
    by_basename: dict[str, list[str]] = {}
    folder_files: list[Path] = []
    if session_folder.is_dir():
        try:
            for path in session_folder.rglob("*"):
                if cancel.is_set():
                    break
                if path.is_file() and not path.is_symlink():
                    resolved = path.resolve()
                    if os.path.commonpath((str(attachment_root.resolve()), str(resolved))) != str(attachment_root.resolve()):
                        continue
                    rel = path.relative_to(session_folder)
                    destination_rel = Path("attachments") / Path(*[safe_name(p, 120) for p in rel.parts])
                    destination = output_root / destination_rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if redact and path.suffix.casefold() in {".txt", ".md", ".json", ".csv", ".log", ".xml", ".yaml", ".yml"}:
                        try:
                            text = path.read_text(encoding="utf-8")
                            text, count = redact_text(text)
                            destination.write_text(text, encoding="utf-8")
                            if redaction_counter is not None:
                                redaction_counter[0] += count
                        except (UnicodeDecodeError, OSError):
                            shutil.copyfile(path, destination)
                    else:
                        shutil.copyfile(path, destination)
                    folder_files.append(path)
                    by_basename.setdefault(path.name.casefold(), []).append(destination_rel.as_posix())
            if not folder_files:
                issues.append("存在会话附件目录，但其中未找到可复制的常规文件")
        except OSError as exc:
            issues.append(f"附件目录读取失败：{type(exc).__name__}: {exc}")

    refs: list[dict[str, Any]] = []
    for event in events:
        refs.extend(event.get("attachments", []))
        data = event.get("data")
        if isinstance(data, dict):
            _gather_refs(data, refs)

    seen_refs: set[str] = set()
    for ref in refs:
        if cancel.is_set():
            break
        value = ref.get("ref")
        if not isinstance(value, str) or not value:
            continue
        if value in seen_refs:
            continue
        seen_refs.add(value)
        if value.startswith("[[ATTACHMENT:"):
            value = value.removeprefix("[[ATTACHMENT:").removesuffix("]]" )
        if value.startswith("data:image/") and ";base64," in value:
            try:
                header, encoded = value.split(",", 1)
                extension = header.split("/", 1)[1].split(";", 1)[0].replace("svg+xml", "svg")
                digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
                rel = Path("attachments") / f"image-{digest}.{safe_name(extension, 10)}"
                destination = output_root / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(base64.b64decode(encoded, validate=False))
                copied[value] = rel.as_posix()
                continue
            except Exception as exc:
                issues.append(f"内嵌图片附件无法解码：{type(exc).__name__}")
                continue
        local_path = _local_path_from_reference(value)
        if local_path and os.path.isfile(local_path):
            try:
                src = Path(local_path)
                digest = hashlib.sha256(str(src.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
                rel = Path("attachments") / f"{safe_name(src.stem, 64)}-{digest}{safe_name(src.suffix, 12)}"
                destination = output_root / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    if redact and src.suffix.casefold() in {".txt", ".md", ".json", ".csv", ".log", ".xml", ".yaml", ".yml"}:
                        try:
                            text = src.read_text(encoding="utf-8")
                            text, count = redact_text(text)
                            destination.write_text(text, encoding="utf-8")
                            if redaction_counter is not None:
                                redaction_counter[0] += count
                        except (UnicodeDecodeError, OSError):
                            shutil.copyfile(src, destination)
                    else:
                        shutil.copyfile(src, destination)
                copied[value] = rel.as_posix()
                continue
            except OSError as exc:
                issues.append(f"明确引用的附件读取失败：{value}：{exc}")
                continue
        basename = Path(value.replace("\\", "/")).name.casefold()
        matches = by_basename.get(basename, [])
        if matches:
            copied[value] = matches[0]
        elif "image" in str(ref.get("kind", "")).casefold():
            image_files = [path for path in folder_files if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}]
            if len(image_files) == 1:
                copied[value] = (Path("attachments") / Path(*[safe_name(p, 120) for p in image_files[0].relative_to(session_folder).parts])).as_posix()
            else:
                issues.append(f"附件未找到或关联不明确：{value[:180]}")
        else:
            issues.append(f"附件未找到：{value[:180]}")
    for path in folder_files:
        key = str(path.resolve())
        rel = Path("attachments") / Path(*[safe_name(p, 120) for p in path.relative_to(session_folder).parts])
        copied.setdefault(key, rel.as_posix())
    return copied


def _gather_refs(value: Any, refs: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in {"image_url", "image_path", "file_path", "attachment_path", "local_image_path"} and isinstance(child, str):
                refs.append({"ref": child, "kind": key})
            else:
                _gather_refs(child, refs)
    elif isinstance(value, list):
        for child in value:
            _gather_refs(child, refs)


def _local_path_from_reference(value: str) -> str:
    if value.casefold().startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if os.name == "nt" and path.startswith("/") and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if parsed.netloc and os.name == "nt":
            path = "\\\\" + parsed.netloc + path.replace("/", "\\")
        return path
    if os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return value
    return ""


def _map_attachments_in_event(event: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    record = dict(event)
    linked = []
    for attachment in event.get("attachments", []):
        ref = attachment.get("ref")
        path = mapping.get(ref) if isinstance(ref, str) else None
        linked.append({"kind": attachment.get("kind", "附件"), "path": path or "", "status": "已导出" if path else "未找到", "reference": ref})
    record["attachments"] = linked
    if isinstance(record.get("text"), str):
        record["text"] = _ATTACHMENT_MARKER.sub(lambda m: mapping.get(m.group(1), "[附件未找到]"), record["text"])
    if isinstance(record.get("data"), (dict, list)):
        record["data"] = _rewrite_attachment_refs(record["data"], mapping)
    return record


def _rewrite_attachment_refs(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if str(key).casefold() in {"image_url", "image_path", "file_path", "attachment_path", "local_image_path"} and isinstance(child, str):
                result[key] = mapping.get(child, "[附件未找到]")
            else:
                result[key] = _rewrite_attachment_refs(child, mapping)
        return result
    if isinstance(value, list):
        return [_rewrite_attachment_refs(item, mapping) for item in value]
    return value


def _chat_events(events: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    if mode == "chat":
        return [event for event in events if event.get("kind") == "message" and event.get("role") in {"user", "assistant"}]
    return events


def _format_txt(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, issues: list[str] | None = None) -> str:
    lines = [f"会话：{session.title}", f"会话 ID：{session.session_id}", f"创建时间（本机）：{session.created_at or '未找到'}", f"创建时间（原始）：{session.raw_created_at or '未找到'}", f"更新时间（本机）：{session.updated_at or '未找到'}", f"工作目录：{session.cwd or '未找到'}", f"是否归档：{'是' if session.archived else '否'}", f"阅读版：{'纯聊天' if mode == 'chat' else '完整版'}", ""]
    for event in _chat_events(events, mode):
        kind = event.get("kind")
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            suffix = f"（{event.get('phase')}）" if event.get("phase") else ""
            text = str(event.get("text", ""))
            text = _ATTACHMENT_MARKER.sub(lambda m: (mapping or {}).get(m.group(1), "[附件未找到]"), text)
            lines.extend([f"[{role}{suffix}]", text, ""])
            for item in event.get("attachments", []):
                reference = item.get("ref")
                path = (mapping or {}).get(reference) if isinstance(reference, str) else None
                lines.append(f"[附件] {path or '未找到'}")
            if event.get("attachments"):
                lines.append("")
        elif kind == "tool_call":
            lines.extend([f"[工具调用] {event.get('name', '未知工具')}  call_id={event.get('call_id') or '未找到'}", str(event.get("text", "")), ""])
        elif kind == "tool_result":
            lines.extend([f"[工具结果] {event.get('name', '工具结果')}  call_id={event.get('call_id') or '未找到'}", str(event.get("text", "")), ""])
        elif kind in {"status", "unknown"}:
            lines.extend([f"[状态 / {event.get('name', '未知类型')}]", str(event.get("text", "")), ""])
    read_issues = session.issues if issues is None else issues
    if read_issues:
        lines.extend(["[读取报告]", *read_issues])
    return "\n".join(lines)


def _format_markdown(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, issues: list[str] | None = None) -> str:
    lines = [f"# {session.title}", "", f"- 会话 ID：`{session.session_id}`", f"- 创建时间（本机）：{session.created_at or '未找到'}", f"- 创建时间（原始）：`{session.raw_created_at or '未找到'}`", f"- 更新时间（本机）：{session.updated_at or '未找到'}", f"- 工作目录：`{session.cwd or '未找到'}`", f"- 归档：{'是' if session.archived else '否'}", f"- 阅读版：{'纯聊天' if mode == 'chat' else '完整版'}", ""]
    for event in _chat_events(events, mode):
        kind = event.get("kind")
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            phase = f"（{event.get('phase')}）" if event.get("phase") else ""
            lines.extend([f"## {role}{phase}", "", str(event.get("text", "")), ""])
            for item in event.get("attachments", []):
                reference = item.get("ref")
                path = item.get("path") or ((mapping or {}).get(reference) if isinstance(reference, str) else None)
                if path:
                    label = str(item.get("kind", "附件")).replace("\\", "\\\\").replace("]", "\\]")
                    destination = quote(str(path), safe="/._-")
                    if Path(str(path)).suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
                        lines.append(f"![{label}]({destination})")
                    else:
                        lines.append(f"[附件：{label}]({destination})")
                else:
                    lines.append("[附件未找到]")
        elif kind in {"tool_call", "tool_result", "status", "unknown"}:
            label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
            heading = f"{label}：{event.get('name', '')}"
            if kind in {"tool_call", "tool_result"}:
                heading += f"（call_id={event.get('call_id') or '未找到'}）"
            lines.extend([f"<details><summary>{heading}</summary>", "", "```text", str(event.get("text", "")), "```", "", "</details>", ""])
    read_issues = session.issues if issues is None else issues
    if read_issues:
        lines.extend(["## 读取报告", "", *[f"- {issue}" for issue in read_issues], ""])
    return "\n".join(lines)


def _format_html(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, issues: list[str] | None = None) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    chunks = []
    for event in _chat_events(events, mode):
        kind = event.get("kind")
        raw_text = str(event.get("text", ""))
        raw_text = _ATTACHMENT_MARKER.sub(lambda m: (mapping or {}).get(m.group(1), "[附件未找到]"), raw_text)
        text = esc(raw_text)
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            phase = f" · {esc(event.get('phase'))}" if event.get("phase") else ""
            attachments = []
            for item in event.get("attachments", []):
                if item.get("path"):
                    rel = esc(item["path"])
                    attachments.append(f'<a href="{rel}">{esc(item.get("kind", "附件"))}</a>')
                else:
                    attachments.append("附件未找到")
            content = text.replace("\n", "<br>\n")
            if attachments:
                content += "<p class=\"attachments\">" + " · ".join(attachments) + "</p>"
            chunks.append(f'<article class="message {"user" if role == "用户" else "assistant"}"><h2>{role}{phase}</h2><div>{content}</div></article>')
        elif kind in {"tool_call", "tool_result", "status", "unknown"}:
            label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
            title = f"{label} · {event.get('name', '')}"
            call_id = event.get("call_id")
            if call_id:
                title += f" · call_id={call_id}"
            chunks.append(f"<details><summary>{esc(title)}</summary><pre>{text}</pre></details>")
    read_issues = session.issues if issues is None else issues
    issue_list = "".join(f"<li>{esc(issue)}</li>" for issue in read_issues)
    meta = " · ".join([f"ID {esc(session.session_id)}", f"创建 {esc(session.created_at or '未找到')}", f"更新 {esc(session.updated_at or '未找到')}", f"工作目录 {esc(session.cwd or '未找到')}", "已归档" if session.archived else "未归档"])
    issue_section = f"<section><h2>读取报告</h2><ul>{issue_list}</ul></section>" if read_issues else ""
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif;max-width:1000px;margin:28px auto;padding:0 18px;color:#202124;background:#fafafa}}
h1{{font-size:24px;margin:0 0 8px}}.meta{{color:#59636d;border-bottom:1px solid #d9dde1;padding:0 0 18px;margin-bottom:20px;overflow-wrap:anywhere}}
article,details,section{{border:1px solid #d7dce0;border-radius:6px;background:white;margin:12px 0;padding:12px 15px;overflow-wrap:anywhere}}
article.user{{border-left:4px solid #168b78}}article.assistant{{border-left:4px solid #4868a8}}h2{{font-size:15px;margin:0 0 8px}}
pre{{white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:65vh;background:#f3f5f6;padding:10px;border-radius:4px}}
summary{{cursor:pointer;font-weight:600}}a{{color:#176e60}}.attachments{{margin-bottom:0}}
</style></head><body><h1>{title}</h1><p class="meta">{meta}</p>{content}{issues}</body></html>""".format(title=esc(session.title), meta=meta, content="\n".join(chunks), issues=issue_section)


def _format_json(session: SessionInfo, events: list[dict[str, Any]], mode: str, redact: bool, mapping: dict[str, str], issues: list[str], counter: list[int]) -> dict[str, Any]:
    selected = [_map_attachments_in_event(event, mapping) for event in _chat_events(events, mode)]
    return {
        "tool_version": APP_VERSION,
        "session": _sanitize(session.public_dict(), redact, counter),
        "reading_mode": mode,
        "events": _sanitize(selected, redact, counter),
        "read_issues": _sanitize(issues, redact, counter),
        "attachment_files": sorted(set(mapping.values())),
    }


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _unique_file(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    index = 2
    while os.path.exists(f"{root}_{index}{ext}"):
        index += 1
    return f"{root}_{index}{ext}"


def export_one(
    session: SessionInfo,
    catalog: Catalog,
    output_root: str,
    formats: list[str],
    mode: str = "full",
    redact: bool = False,
    include_raw: bool = False,
    cancel=None,
    progress: Callable[[str, int], None] | None = None,
    include_source_paths: bool = True,
) -> dict[str, Any]:
    import threading

    cancel = cancel or threading.Event()
    os.makedirs(output_root, exist_ok=True)
    safe_title = redact_text(session.title)[0] if redact else session.title
    name = safe_name(safe_title) + "__" + safe_name(session.session_id, 50)
    folder = unique_directory(output_root, name)
    marker = os.path.join(folder, "未完成.txt")
    Path(marker).write_text("导出尚未完成。请检查导出报告；此目录可能只包含部分文件。\n", encoding="utf-8")
    issues = list(session.issues)
    redaction_counter = [0]
    zip_path = ""
    try:
        if progress:
            progress(f"读取会话：{session.title[:60]}", -1)
        events, load_issues, record_counts = load_session_events(session, catalog, cancel)
        issues.extend(x for x in load_issues if x not in issues)
        if cancel.is_set():
            return {"session_id": session.session_id, "folder": folder, "status": "已取消", "issues": issues, "files": []}

        mapping = _copy_related_attachments(session, catalog, folder, events, cancel, issues, redact, redaction_counter)
        selected_formats = {str(x).casefold() for x in formats}
        if "zip" in selected_formats:
            selected_formats.update({"json"})
        if not selected_formats.intersection({"txt", "md", "html", "json"}) and "zip" in selected_formats:
            selected_formats.add("json")
        if "txt" in selected_formats:
            text = _format_txt(session, events, mode, mapping, issues)
            if redact:
                text, n = redact_text(text)
                redaction_counter[0] += n
            Path(folder, "聊天记录.txt").write_text(text, encoding="utf-8")
        if "md" in selected_formats:
            text = _format_markdown(session, events, mode, mapping, issues)
            text = _ATTACHMENT_MARKER.sub(lambda m: mapping.get(m.group(1), "[附件未找到]"), text)
            if redact:
                text, n = redact_text(text)
                redaction_counter[0] += n
            Path(folder, "聊天记录.md").write_text(text, encoding="utf-8")
        if "html" in selected_formats:
            text = _format_html(session, events, mode, mapping, issues)
            if redact:
                text, n = redact_text(text)
                redaction_counter[0] += n
            Path(folder, "聊天记录.html").write_text(text, encoding="utf-8")
        if "json" in selected_formats:
            payload = _format_json(session, events, mode, redact, mapping, issues, redaction_counter)
            Path(folder, "聊天记录.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if include_raw:
            raw_root = Path(folder) / "原始文件"
            raw_root.mkdir(parents=True, exist_ok=True)
            raw_found = False
            for source in ([] if session.source == "导入 ZIP" else session.paths):
                if cancel.is_set():
                    break
                if not os.path.isfile(source) or " :: " in source:
                    continue
                raw_found = True
                destination = _unique_file(str(raw_root / Path(source).name))
                shutil.copyfile(source, destination)
            if not raw_found:
                message = "导入包不包含原始 rollout 文件；该会话没有可复制的原始 JSONL" if session.source == "导入 ZIP" else "原始 rollout 文件未找到；该会话没有可复制的原始 JSONL"
                issues.append(message)
        if cancel.is_set():
            return {"session_id": session.session_id, "folder": folder, "status": "已取消", "issues": issues, "files": []}

        generated_files = []
        for base, _, filenames in os.walk(folder):
            for filename in filenames:
                path = os.path.join(base, filename)
                if path == marker or filename == "manifest.json":
                    continue
                digest, size = _hash_file(path)
                generated_files.append({"path": os.path.relpath(path, folder).replace(os.sep, "/"), "sha256": digest, "size_bytes": size})
        manifest_title = _sanitize(session.title, redact, redaction_counter)
        manifest_source_paths = _sanitize(session.paths, redact, redaction_counter) if include_source_paths else []
        manifest_issues = _sanitize(issues, redact, redaction_counter)
        result_title = _sanitize(session.title, redact, redaction_counter)
        result_issues = _sanitize(issues, redact, redaction_counter)
        manifest = {
            "tool_version": APP_VERSION,
            "exported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "session_id": session.session_id,
            "title": manifest_title,
            "source_paths": manifest_source_paths,
            "source": session.source,
            "reading_mode": mode,
            "sensitive_content_redacted": bool(redact),
            "redacted_occurrences": redaction_counter[0],
            "raw_source_included_without_redaction": bool(include_raw),
            "record_counts": {
                "user_messages": session.user_count,
                "assistant_messages": session.assistant_count,
                "tool_calls": session.tool_count,
                "normalized_events": len(events),
                "source_types": dict(record_counts or session.type_counts),
                "duplicate_records_skipped_in_reading_view": session.duplicate_count,
            },
            "files": generated_files,
            "missing_content_and_errors": manifest_issues,
            "manifest_hash_note": "为避免自引用，文件清单中的 SHA-256 不包含本 manifest。",
        }
        Path(folder, "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.remove(marker)
        if "zip" in selected_formats:
            zip_path = _unique_file(os.path.join(output_root, name + ".zip"))
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for base, _, filenames in os.walk(folder):
                    for filename in filenames:
                        path = os.path.join(base, filename)
                        arcname = os.path.join(os.path.basename(folder), os.path.relpath(path, folder))
                        archive.write(path, arcname.replace(os.sep, "/"))
        if progress:
            progress(f"完成：{session.title[:60]}", -1)
        result = {"session_id": session.session_id, "title": result_title, "folder": folder, "zip": zip_path, "status": "完成" if not issues else "完成（有报告项）", "issues": result_issues, "files": generated_files}
        return result
    except Exception as exc:
        issues.append(f"导出失败：{type(exc).__name__}: {exc}")
        if not os.path.exists(marker):
            Path(marker).write_text("导出失败或未完成。\n" + "\n".join(issues), encoding="utf-8")
        return {"session_id": session.session_id, "title": session.title, "folder": folder, "zip": zip_path, "status": "失败", "issues": issues, "files": []}


def export_batch(
    sessions: list[SessionInfo],
    catalog: Catalog,
    output_root: str,
    formats: list[str],
    mode: str,
    redact: bool,
    include_raw: bool,
    cancel,
    progress: Callable[[str, int], None] | None = None,
    result_callback: Callable[[dict[str, Any]], None] | None = None,
    include_source_paths: bool = True,
) -> list[dict[str, Any]]:
    results = []
    total = len(sessions)
    for index, session in enumerate(sessions, 1):
        if cancel.is_set():
            break

        def one_progress(message: str, _pct: int) -> None:
            if progress:
                progress(f"{index}/{total}：{message}", int((index - 1) * 100 / max(total, 1)))

        result = export_one(
            session,
            catalog,
            output_root,
            formats,
            mode,
            redact,
            include_raw,
            cancel,
            one_progress,
            include_source_paths,
        )
        results.append(result)
        if result_callback:
            result_callback(result)
        if progress:
            progress(f"已处理 {index}/{total} 个会话", int(index * 100 / max(total, 1)))
    report_path = _unique_file(os.path.join(output_root, "导出报告.json"))
    report = {
        "tool_version": APP_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cancelled": cancel.is_set(),
        "requested_sessions": len(sessions),
        "processed_sessions": len(results),
        "results": results,
    }
    try:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return results


def export_migration_bundle(
    sessions: list[SessionInfo],
    catalog: Catalog,
    output_path: str,
    cancel,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    output_path = os.path.abspath(output_path)
    output_root = os.path.dirname(output_path)
    os.makedirs(output_root, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="codex-history-migration-") as work_root:
        results = export_batch(
            sessions,
            catalog,
            work_root,
            ["zip"],
            "full",
            False,
            False,
            cancel,
            progress,
            include_source_paths=False,
        )
        if cancel.is_set():
            return {"path": "", "exported": 0, "failed": 0, "cancelled": True, "results": results}

        successful = [
            result
            for result in results
            if result.get("status", "").startswith("完成")
            and result.get("zip")
            and os.path.isfile(result["zip"])
        ]
        if not successful:
            raise RuntimeError("没有会话成功导出，未生成迁移包。")

        entries = []
        for index, result in enumerate(successful, 1):
            digest, size = _hash_file(result["zip"])
            entries.append(
                {
                    "archive": f"sessions/{index:06d}.zip",
                    "session_id": result["session_id"],
                    "title": result.get("title") or "",
                    "sha256": digest,
                    "size_bytes": size,
                }
            )

        manifest = {
            "format": "codex-history-viewer-migration",
            "version": 1,
            "tool_version": APP_VERSION,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "session_count": len(entries),
            "failed_count": len(results) - len(successful),
            "sessions": entries,
        }

        fd, partial_path = tempfile.mkstemp(
            prefix=f".{Path(output_path).stem}.",
            suffix=".partial",
            dir=output_root,
        )
        os.close(fd)
        try:
            cancelled = False
            with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_STORED) as bundle:
                bundle.writestr(
                    "bundle_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                )
                for index, (entry, result) in enumerate(zip(entries, successful), 1):
                    if cancel.is_set():
                        cancelled = True
                        break
                    copied_hash = hashlib.sha256()
                    copied_size = 0
                    with open(result["zip"], "rb") as source, bundle.open(entry["archive"], "w", force_zip64=True) as target:
                        while True:
                            if cancel.is_set():
                                cancelled = True
                                break
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
                            copied_hash.update(block)
                            copied_size += len(block)
                    if cancelled:
                        break
                    if copied_size != entry["size_bytes"] or copied_hash.hexdigest() != entry["sha256"]:
                        raise OSError(f"会话包在整理时发生变化：{entry['session_id']}")
                    if progress:
                        progress(f"正在打包会话 {index}/{len(successful)}", int(index * 100 / len(successful)))

            if cancelled:
                return {
                    "path": "",
                    "exported": 0,
                    "failed": len(results) - len(successful),
                    "cancelled": True,
                    "results": results,
                }
            os.replace(partial_path, output_path)
            partial_path = ""
        finally:
            if partial_path and os.path.exists(partial_path):
                os.unlink(partial_path)

    return {
        "path": output_path,
        "exported": len(successful),
        "failed": len(results) - len(successful),
        "cancelled": False,
        "results": results,
    }

