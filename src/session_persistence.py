"""
会话持久化模块：JSONL 落盘 + 回放 + 列出历史会话。

对应 TS 源码：
    claude-code-main/src/utils/sessionStorage.ts          —— JSONL append / read / Project class
    claude-code-main/src/utils/sessionStoragePortable.ts  —— sanitizePath、readHeadAndTail、lite read
    claude-code-main/src/utils/listSessionsImpl.ts        —— listSessions（head/tail 轻量扫描）

核心设计（详见 docs/session_persistence.md）：
    存储目录：~/.my_cc/projects/<sanitize(cwd)>/<sessionId>.jsonl
    · JSONL 一行一条 Entry，只追加（append-only），不重写整文件
    · Entry 包裹 Anthropic message，额外带 uuid / sessionId / cwd / timestamp
    · 列历史会话时不读全文件，只读头尾各 64KB 提取首句 + 摘要（= lite read）
    · --continue = 自动找本项目最近修改的 session；--resume = 列出所有让用户挑
"""

from __future__ import annotations

import os
import re
import sys
import json
import uuid as _uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from pydantic import BaseModel, Field

# =============================================================================
# 一、路径工具 —— 对应 TS sanitizePath / getProjectDir / getTranscriptPath
# =============================================================================

MAX_SANITIZED_LENGTH = 200


def _config_home() -> Path:
    """~/.my_cc 目录。设 MY_CC_HOME 环境变量可覆盖（方便测试）。"""
    if (override := os.environ.get("MY_CC_HOME")):
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", ".")) / ".my_cc"
    return Path(os.environ.get("HOME", ".")) / ".my_cc"


def sanitize_path(name: str) -> str:
    """
    把任意路径字符串变成「安全的目录名」：非字母数字转 -，超长则截断 + hash 后缀。
    对应 TS sessionStoragePortable.ts 的 sanitizePath。
    这是按 cwd 找到「本项目专属目录」的关键函数——不同项目的会话不会混在一起。
    """
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    # 超长路径（Windows 盘符 + 深层目录）：截断后保留 hash 防冲突
    hash_suffix = str(abs(hash(name)) % (36 ** 4))
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{hash_suffix}"


def get_projects_dir() -> Path:
    """~/.my_cc/projects —— 所有项目会话的根目录。对应 TS getProjectsDir。"""
    return _config_home() / "projects"


def get_project_dir(cwd: str) -> Path:
    """
    根据当前工作目录算出「本项目专属的会话目录」。
    例：E:\repo → ~/.my_cc/projects/E--repo
    对应 TS getProjectDir（memoize 的便捷包装）。
    """
    return get_projects_dir() / sanitize_path(os.path.abspath(cwd))


def get_transcript_path(session_id: str, project_dir: Path) -> Path:
    """一个会话的 JSONL 文件路径。对应 TS getTranscriptPath。"""
    return project_dir / f"{session_id}.jsonl"


def validate_uuid(s: str) -> Optional[str]:
    """校验 UUID 格式（8-4-4-4-12 hex），不合法返回 None。对应 TS validateUuid。"""
    m = re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s, re.I
    )
    return m.group(0) if m else None


# =============================================================================
# 二、JSONL 读写 —— 对应 TS Project.appendEntry / insertMessageChain
# =============================================================================


def _make_entry(
    entry_type: str, message: Dict, session_id: str, cwd: str
) -> Dict:
    """
    把一条 Anthropic message 包裹成一个 JSONL Entry。
    Entry = message + 元数据（type / uuid / sessionId / cwd / timestamp），
    和真 CC 的 TranscriptMessage 字段对齐（删掉了 gitBranch / version / parentUuid 这些我们暂不需要的）。

    ★ uuid 生成策略：如果 message 本身已经带了 "uuid" 字段（由 QueryEngine 在创建消息时预生成），
       就复用——这样多次 save（如 /resume 的「存旧 → 加载新 → 再存」）不会因为 uuid 变化而重复写。
       否则现场生成一个。
    """
    msg_uuid = message.get("uuid") or _uuid.uuid4().hex
    return {
        "type": entry_type,        # "user" | "assistant"
        "uuid": msg_uuid,
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": message,
    }


def _parse_jsonl(content: str) -> List[Dict]:
    """把整个 JSONL 文本解析成 Entry 列表（跳过空行 / JSON 损坏行）。"""
    entries: List[Dict] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def load_transcript(file_path: Path) -> List[Dict]:
    """读完整 JSONL 文件，返回 Entry 列表。MVP 读全文件（不处理 GB 级）。"""
    if not file_path.exists():
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return _parse_jsonl(f.read())
    except OSError:
        return []


def _read_existing_uuids(file_path: Path) -> set:
    """扫已有 JSONL 文件，收集所有 entry 的 uuid（去重用）。"""
    uuids = set()
    if not file_path.exists():
        return uuids
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if "uuid" in entry:
                        uuids.add(entry["uuid"])
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return uuids


def save_messages(
    session_id: str,
    project_dir: Path,
    cwd: str,
    messages: List[Dict],
    *,
    _checkpoint_index: int = 0,
) -> int:
    """
    把 messages 列表（自上次保存之后新增的部分）追加写进 JSONL。
    返回本次保存后的新 checkpoint_index（= len(messages)）。

    只用 `_checkpoint_index` 切出增量——不改 messages 本身、也不在文件上加锁。
    和真 CC 的 `recordTranscript` 一样，用 uuid 去重兜底，重复写不会出两份。
    """
    new_msgs = messages[_checkpoint_index:]
    if not new_msgs:
        return _checkpoint_index

    project_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_transcript_path(session_id, project_dir)

    existing_uuids = _read_existing_uuids(file_path)
    lines: List[str] = []
    for msg in new_msgs:
        entry = _make_entry(
            entry_type=msg.get("role", "user"),
            message=msg,
            session_id=session_id,
            cwd=cwd,
        )
        if entry["uuid"] in existing_uuids:
            continue
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
        existing_uuids.add(entry["uuid"])

    if lines:
        with open(file_path, "a", encoding="utf-8") as f:
            f.writelines(lines)

    return len(messages)


# =============================================================================
# 三、轻量读取（lite read）—— 头/尾各 64KB，只取首句 + 标题
# =============================================================================
#
# 对应 TS 的 readHeadAndTail / readSessionLite / extractFirstPromptFromHead /
# extractJsonStringField / extractLastJsonStringField。
# 关键性能取舍：--resume 列 N 个会话 ≠ 读 N 个完整 GB 文件——只读头 + 尾各 64KB，
# 从中提取首句和最近一条用户话语当摘要，读写量是常数。

LITE_READ_BUF_SIZE = 65536  # 64KB


def _read_head_tail(file_path: Path) -> Optional[Dict[str, str]]:
    """读文件头 + 尾各 LITE_READ_BUF_SIZE 字节。返回 {"head", "tail"}。"""
    try:
        size = file_path.stat().st_size
        if size == 0:
            return None
        with open(file_path, "rb") as f:
            head = f.read(LITE_READ_BUF_SIZE).decode("utf-8", errors="replace")
            tail = head
            if size > LITE_READ_BUF_SIZE:
                f.seek(-LITE_READ_BUF_SIZE, os.SEEK_END)
                tail = f.read(LITE_READ_BUF_SIZE).decode("utf-8", errors="replace")
        return {"head": head, "tail": tail}
    except OSError:
        return None


def _extract_string_field(text: str, key: str, *, last: bool = False) -> Optional[str]:
    """
    从原始 JSON 文本里提取 `"key":"value"` 的 value，不依赖完整 JSON parse。
    这样即使 tail 的第一行是残缺的，也能从后面的行提取到字段。
    对应 TS 的 extractJsonStringField / extractLastJsonStringField。
    """
    patterns = [f'"{key}":"', f'"{key}": "']
    best: Optional[str] = None
    for pattern in patterns:
        search_from = 0
        while True:
            idx = text.find(pattern, search_from)
            if idx < 0:
                break
            val_start = idx + len(pattern)
            i = val_start
            escaped = False
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    raw = text[val_start:i]
                    try:
                        best = json.loads(f'"{raw}"')
                    except json.JSONDecodeError:
                        best = raw
                    if not last:
                        return best  # first match → done
                    break  # last match → continue scanning
                i += 1
            search_from = i + 1
    return best


def _extract_first_prompt(head: str) -> str:
    """
    从 head chunk 里找到第一个有意义的 user 消息文本。
    跳过斜线命令（/xxx）、空消息、isMeta 标记消息。
    对应 TS 的 extractFirstPromptFromHead。
    """
    lines = head.split("\n")
    for line in lines:
        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        if '"isMeta":true' in line or '"isMeta": true' in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content", [])
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
        else:
            continue
        for t in texts:
            t = t.strip()
            if not t:
                continue
            # 跳过斜线命令
            if t.startswith("/"):
                continue
            t = t.replace("\n", " ")
            if len(t) > 200:
                t = t[:200].rstrip() + "…"
            return t
    return ""


def _extract_last_user_message(tail: str) -> str:
    """
    从 tail chunk 倒着找最后一条 user 消息文本（给 --resume 列表当「最近在聊什么」）。
    """
    lines = tail.split("\n")
    for line in reversed(lines):
        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content", [])
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = " ".join(t.strip() for t in texts if t.strip())
        else:
            continue
        if text:
            if len(text) > 200:
                text = text[:200].rstrip() + "…"
            return text
    return ""


# =============================================================================
# 四、SessionInfo —— --resume 列表里每行展示的信息
# =============================================================================

class SessionInfo(BaseModel):
    """一个历史会话的摘要信息（不加载完整 JSONL）。对应 TS listSessionsImpl 的 SessionInfo。"""
    session_id: str = Field(description="UUID，同时也是 .jsonl 的文件名")
    summary: str = Field(description="列表显示的摘要行：title → lastPrompt → firstPrompt 降级链")
    last_modified: float = Field(description="文件 mtime，用于排序（最新在前）")
    file_size: int = Field(default=0, description="字节数")
    custom_title: Optional[str] = Field(default=None, description="用户手动设的标题（/rename）")
    first_prompt: Optional[str] = Field(default=None, description="本会话第一句有意义的话")
    cwd: Optional[str] = Field(default=None, description="会话当时的工作目录")


# =============================================================================
# 五、列出历史会话 + 找最近 —— 对应 TS listSessionsImpl
# =============================================================================


async def list_sessions(
    project_dir: Path,
    *,
    limit: int = 20,
) -> List[SessionInfo]:
    """
    列出指定项目目录下所有历史会话，按 mtime 最新在前。
    不读整个 JSONL——只读头尾各 64KB，从里面取首句 / 最近一句当摘要。
    """
    if not project_dir.exists():
        return []

    # 收集候选文件（只收 UUID 命名的 .jsonl）
    candidates: List[Tuple[str, Path, float, int]] = []
    for entry in project_dir.iterdir():
        if not entry.name.endswith(".jsonl"):
            continue
        sid = validate_uuid(entry.name[:-6])  # strip .jsonl
        if not sid:
            continue
        try:
            stat = entry.stat()
            candidates.append((sid, entry, stat.st_mtime, stat.st_size))
        except OSError:
            continue

    # 按 mtime 降序
    candidates.sort(key=lambda x: x[2], reverse=True)

    sessions: List[SessionInfo] = []
    for sid, fpath, mtime, fsize in candidates[:limit]:
        lite = _read_head_tail(fpath)
        if not lite:
            continue
        head, tail = lite["head"], lite["tail"]

        first_prompt = _extract_first_prompt(head) or None
        title = _extract_string_field(tail, "customTitle") or _extract_string_field(tail, "aiTitle")
        last_prompt = _extract_string_field(tail, "lastPrompt")
        cwd_val = _extract_string_field(head, "cwd")

        # 摘要降级链：手动标题 → 最近一句话 → 首句 → 空占位
        summary = title or last_prompt or first_prompt or "(空会话)"

        sessions.append(SessionInfo(
            session_id=sid,
            summary=summary,
            last_modified=mtime,
            file_size=fsize,
            custom_title=title or None,
            first_prompt=first_prompt,
            cwd=cwd_val,
        ))

    return sessions


async def find_most_recent_session(project_dir: Path) -> Optional[str]:
    """找出本项目最近修改的会话 ID（供 --continue）。"""
    sessions = await list_sessions(project_dir, limit=1)
    return sessions[0].session_id if sessions else None


# =============================================================================
# 六、重命名（custom-title）—— 对应 TS saveCustomTitle / saveAiGeneratedTitle
# =============================================================================
#
# 真 CC 把 custom-title 当 metadata entry 追加到 JSONL 尾部（和 reAppendSessionMetadata
# 同理：保证在 tail 64KB 窗口内，--resume 选单随手可读）。
# 用户手动设的 customTitle 优先级高于 AI 自动生成的 aiTitle（读者先读 customTitle）。


def set_custom_title(
    session_id: str,
    project_dir: Path,
    title: str,
    cwd: str = "",
) -> None:
    """给指定会话设一个自定义标题。写入一条 metadata entry 到 JSONL 尾部。

    标题放在尾部保证 --resume 选单的 lite read（只读尾 64KB）必然能扫到它。
    空标题等价于「清除标题」——真 CC 也允许空字符串 customTitle。
    """
    file_path = get_transcript_path(session_id, project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "custom-title",
        "uuid": _uuid.uuid4().hex,
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "customTitle": title,
    }
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# =============================================================================
# 七、删除会话 —— 删除整个 .jsonl 文件
# =============================================================================


def delete_session(session_id: str, project_dir: Path) -> bool:
    """
    删除一个会话的 JSONL 文件。成功返回 True，失败（如文件不存在）返回 False。

    这是用户手动删除会话的唯一入口——不回收站、不软删除，直接删文件。
    对应真 CC 没有这个功能（真 CC 靠 cleanup.ts 按期自动清理旧会话，用户无法手动删）。
    """
    file_path = get_transcript_path(session_id, project_dir)
    try:
        if file_path.exists():
            file_path.unlink()
            return True
        return False
    except OSError:
        return False


# =============================================================================
# 八、从 JSONL 恢复 messages —— 供 QueryEngine 加载历史
# =============================================================================


async def load_session_messages(
    project_dir: Path, session_id: str
) -> List[Dict]:
    """
    从 JSONL 里读出 messages 列表（还原成 QueryEngine.messages 的格式）。
    只提取 type==user 或 assistant 的 entry，取其 message 字段。
    跳过元数据 entry（custom-title / tag / …）。
    """
    file_path = get_transcript_path(session_id, project_dir)
    entries = load_transcript(file_path)

    messages: List[Dict] = []
    for entry in entries:
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        # 确保 role 字段和 type 一致（兜底）
        if "role" not in msg:
            msg = dict(msg, role=entry_type)
        messages.append(msg)
    return messages
