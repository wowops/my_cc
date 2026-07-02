"""
session_persistence.py 的回归 / 演示脚本。

覆盖核心函数（sanitize_path / write-read / dedup / lite read / list / load），
带断言——失败了会停并告诉你哪里不对，方便以后 debug。

运行：python my_cc/demos/session_persistence_demo.py
"""

import os
import sys
import json
import asyncio
import uuid
import tempfile
import shutil
from pathlib import Path

# 让 demo 目录里的 import src/ 下的模块可行
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from session_persistence import (
    sanitize_path,
    validate_uuid,
    get_project_dir,
    get_projects_dir,
    get_transcript_path,
    load_transcript,
    save_messages,
    load_session_messages,
    find_most_recent_session,
    list_sessions,
    _extract_first_prompt,
    _extract_string_field,
    _extract_last_user_message,
)

PASS = 0
FAIL = 0


def check(desc: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {desc}")
    else:
        FAIL += 1
        print(f"  ❌ {desc}")


# =============================================================================
# 测试 1：路径工具
# =============================================================================
print("━" * 64)
print("测试 1：sanitize_path / validate_uuid / 路径构造")
print("━" * 64)

result = sanitize_path("E:/university/SOFTWARE/python")
check("Windows 盘符 + 路径 → 字母数字 + 横线", result == "E--university-SOFTWARE-python")

long_path = "/" + "a" * 250 + "/" + "b" * 250
result = sanitize_path(long_path)
check("超长路径截断（200 + '-' + 短 hash）",
      len(result) >= 200 and len(result) <= 210 and result.startswith("-"))

check("合法 UUID 通过校验", validate_uuid(str(uuid.uuid4())) is not None)
check("非 UUID 字符串返回 None", validate_uuid("not-a-uuid") is None)
check("空字符串返回 None", validate_uuid("") is None)

proj_dir = get_project_dir("/home/user/my-project")
check("get_project_dir 以 projects 目录结尾", "projects" in str(proj_dir))
check("get_project_dir 包含 sanitize 后的 cwd", sanitize_path("/home/user/my-project") in str(proj_dir))

transcript = get_transcript_path("abc-123", Path("/tmp/test"))
check("get_transcript_path 文件名是 uuid.jsonl", transcript.name == "abc-123.jsonl")

# =============================================================================
# 测试 2：写 → 读 → 去重（同步 + async 两条路径）
# =============================================================================
print("\n" + "━" * 64)
print("测试 2：save_messages / load_transcript / JSONL 去重 + 增量追加")
print("━" * 64)

tmpdir = Path(tempfile.mkdtemp())
sid = str(uuid.uuid4())
file_path = get_transcript_path(sid, tmpdir)

msg_uuids = [uuid.uuid4().hex for _ in range(4)]
msgs = [
    {"role": "user", "content": [{"type": "text", "text": "你好"}], "uuid": msg_uuids[0]},
    {"role": "assistant", "content": [{"type": "text", "text": "你好！需要什么帮助？"}], "uuid": msg_uuids[1]},
]

# 2a) 首次写入
save_messages(sid, tmpdir, cwd="/test", messages=msgs)
entries = load_transcript(file_path)
check("首次写 2 条 message → JSONL 有 2 个 entry", len(entries) == 2)
check("entry[0] type=user", entries[0]["type"] == "user")
check("entry[1] type=assistant", entries[1]["type"] == "assistant")
check("entry 含 uuid", "uuid" in entries[0] and "uuid" in entries[1])
check("entry 含 sessionId", entries[0]["sessionId"] == sid)

# 2b) 重复写同一批（uuid 相同，应去重——count 不变）
save_messages(sid, tmpdir, cwd="/test", messages=msgs)
entries = load_transcript(file_path)
check("重复写相同 uuid → 仍为 2 条（去重生效）", len(entries) == 2)

# 2c) 增量追加：前 2 条已存 + 后 2 条新的
msgs_extended = [
    *msgs,
    {"role": "user", "content": [{"type": "text", "text": "写一个排序函数"}], "uuid": msg_uuids[2]},
    {"role": "assistant", "content": [{"type": "text", "text": "好的，给你写一个排序函数。"}], "uuid": msg_uuids[3]},
]
save_messages(sid, tmpdir, cwd="/test", messages=msgs_extended, _checkpoint_index=2)
entries = load_transcript(file_path)
check("增量追加后 → 共 4 条", len(entries) == 4)
check("第 3 条是 user（排序函数）", "写一个排序函数" in entries[2]["message"]["content"][0]["text"])

# 2d) 没有 uuid 的消息也能存（现场生成）
msg_no_uuid = [{"role": "user", "content": [{"type": "text", "text": "无 uuid 测试"}]}]
save_messages(sid, tmpdir, cwd="/test", messages=msg_no_uuid)
entries = load_transcript(file_path)
check("无 uuid 消息也能存", len(entries) == 5)
check("自动生成了 uuid", "uuid" in entries[4])

shutil.rmtree(tmpdir)

# =============================================================================
# 测试 3：async load_session_messages（还原成 messages 列表）
# =============================================================================
print("\n" + "━" * 64)
print("测试 3：load_session_messages → 还原 QueryEngine.messages 格式")
print("━" * 64)

tmpdir = Path(tempfile.mkdtemp())
sid = str(uuid.uuid4())
msgs = [
    {"role": "user", "content": [{"type": "text", "text": "你好"}], "uuid": uuid.uuid4().hex},
    {"role": "assistant", "content": [{"type": "text", "text": "你好！"}], "uuid": uuid.uuid4().hex},
    {"role": "user", "content": [{"type": "text", "text": "再见"}], "uuid": uuid.uuid4().hex},
]
save_messages(sid, tmpdir, cwd="/test", messages=msgs)

loaded = asyncio.run(load_session_messages(tmpdir, sid))
check("加载回 3 条 message", len(loaded) == 3)
check("都是 user 或 assistant", all(m["role"] in ("user", "assistant") for m in loaded))
check("第一条 hello 文本正确", loaded[0]["content"][0]["text"] == "你好")
check("保留 role 字段", loaded[1]["role"] == "assistant")

# 空会话文件
empty_dir = Path(tempfile.mkdtemp())
empty_sid = str(uuid.uuid4())
empty_loaded = asyncio.run(load_session_messages(empty_dir, empty_sid))
check("不存在的会话返回空列表", empty_loaded == [])

shutil.rmtree(tmpdir)
shutil.rmtree(empty_dir)

# =============================================================================
# 测试 4：lite read（头/尾 64KB 提取，不读全文件）
# =============================================================================
print("\n" + "━" * 64)
print("测试 4：_extract_first_prompt / _extract_string_field / _extract_last_user_message")
print("━" * 64)

# 4a) 首句提取：正常用户消息
head_line = json.dumps({
    "type": "user", "uuid": "x", "sessionId": "s", "cwd": "/t",
    "timestamp": "2025-01-01T00:00:00Z",
    "message": {"role": "user", "content": [{"type": "text", "text": "帮我写一个排序函数"}]},
})
prompt = _extract_first_prompt(head_line)
check("提取首句 '帮我写一个排序函数'", "排序" in prompt)

# 4b) 首句提取：斜线命令被跳过
head_with_cmd = (
    json.dumps({
        "type": "user", "uuid": "x", "sessionId": "s", "cwd": "/t",
        "timestamp": "2025-01-01T00:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "/clear"}]},
    }) + "\n" +
    json.dumps({
        "type": "user", "uuid": "y", "sessionId": "s", "cwd": "/t",
        "timestamp": "2025-01-01T00:00:01Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "真正的对话开始"}]},
    })
)
prompt2 = _extract_first_prompt(head_with_cmd)
check("斜线命令被跳过，取到真正的对话", "真正的对话开始" in prompt2)

# 4c) 自定义标题字段提取
tail = '{"type":"custom-title","customTitle":"My Session","sessionId":"s"}'
title = _extract_string_field(tail, "customTitle")
check("从 JSON 尾部提取 customTitle", title == "My Session")

# 4d) 尾句提取（最近一条用户消息）
tail_with_msgs = (
    json.dumps({"type": "user", "uuid": "a", "sessionId": "s", "cwd": "/t",
                "timestamp": "2025-06-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "第一条消息"}]}}) + "\n" +
    json.dumps({"type": "assistant", "uuid": "b", "sessionId": "s", "cwd": "/t",
                "timestamp": "2025-06-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "回复第一条"}]}}) + "\n" +
    json.dumps({"type": "user", "uuid": "c", "sessionId": "s", "cwd": "/t",
                "timestamp": "2025-06-01T00:00:02Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "最新的一条消息"}]}})
)
last = _extract_last_user_message(tail_with_msgs)
check("尾部取到最后一条 user 消息", "最新的一条消息" in last)

# =============================================================================
# 测试 5：list_sessions + find_most_recent（lite read 聚合）
# =============================================================================
print("\n" + "━" * 64)
print("测试 5：list_sessions / find_most_recent_session（读头/尾 64KB）")
print("━" * 64)

tmpdir = Path(tempfile.mkdtemp())

# 制造 3 个会话，让它们有不同的 mtime
import time

for i, (text, delay) in enumerate([
    ("第一个会话", 0.15),
    ("第二个会话", 0.15),
    ("第三个会话（最新）", 0.15),
]):
    s = str(uuid.uuid4())
    save_messages(s, tmpdir, cwd="/test",
                  messages=[{"role": "user",
                             "content": [{"type": "text", "text": text}],
                             "uuid": uuid.uuid4().hex}])
    time.sleep(delay)

sessions = asyncio.run(list_sessions(tmpdir))
check("找到 3 个会话", len(sessions) == 3)
check("按 mtime 最新在前", "第三个" in sessions[0].summary)
check("第一个会话排在最后", "第一个" in sessions[-1].summary)
check("每个 SessionInfo 都有 session_id", all(s.session_id for s in sessions))
check("first_prompt 非空", all(s.first_prompt for s in sessions))

most_recent = asyncio.run(find_most_recent_session(tmpdir))
check("find_most_recent 返回最新会话", most_recent == sessions[0].session_id)

# 空目录
empty_dir = Path(tempfile.mkdtemp())
empty_sessions = asyncio.run(list_sessions(empty_dir))
check("空目录返回空列表", empty_sessions == [])

check("空目录 find_most_recent 返回 None",
      asyncio.run(find_most_recent_session(empty_dir)) is None)

shutil.rmtree(tmpdir)
shutil.rmtree(empty_dir)

# =============================================================================
# 测试 6：边界情况
# =============================================================================
print("\n" + "━" * 64)
print("测试 6：边界 / 异常情况")
print("━" * 64)

# 6a) 损坏的 JSONL 行不应崩溃
tmpdir = Path(tempfile.mkdtemp())
sid = str(uuid.uuid4())
fpath = get_transcript_path(sid, tmpdir)
tmpdir.mkdir(parents=True, exist_ok=True)
with open(fpath, "w", encoding="utf-8") as f:
    f.write('{"type":"user","uuid":"a","sessionId":"' + sid + '","cwd":"/t","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"正常行"}]}}\n')
    f.write("这不是合法的 JSON\n")  # 损坏行
    f.write('{"type":"assistant","uuid":"b","sessionId":"' + sid + '","cwd":"/t","timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"还是正常的"}]}}\n')

entries = load_transcript(fpath)
check("损坏行被跳过（2 条有效 entry）", len(entries) == 2)
check("第一条正常", entries[0]["type"] == "user")
check("第二条正常（跳过损坏行后仍能读到）", entries[1]["type"] == "assistant")

loaded = asyncio.run(load_session_messages(tmpdir, sid))
check("load_session_messages 也跳过损坏行", len(loaded) == 2)

# 6b) 不存在的文件
check("不存在的文件 load_transcript 返回空列表", load_transcript(Path("/nonexistent/abc.jsonl")) == [])

# 6c) 只有非 user/assistant 的 entry
tmpdir2 = Path(tempfile.mkdtemp())
sid2 = str(uuid.uuid4())
save_messages(sid2, tmpdir2, cwd="/test",
              messages=[])  # 建个空文件
# 手动写一条纯元数据行（不含 message 字段的那种）
fpath2 = get_transcript_path(sid2, tmpdir2)
with open(fpath2, "a", encoding="utf-8") as f:
    f.write('{"type":"custom-title","customTitle":"测试标题","sessionId":"' + sid2 + '"}\n')
loaded2 = asyncio.run(load_session_messages(tmpdir2, sid2))
check("纯元数据 entry 被 load_session_messages 跳过", len(loaded2) == 0)

# 6d) delete_session —— 删文件 + 删不存在的会话
from session_persistence import delete_session

# 先确认文件存在
fpath_del = get_transcript_path(sid2, tmpdir2)
check("删除前文件存在", fpath_del.exists())
ok = delete_session(sid2, tmpdir2)
check("delete_session 返回 True（成功删除）", ok is True)
check("删除后文件不存在", not fpath_del.exists())
# 删不存在的会话
ok2 = delete_session("nonexistent-id-1234", tmpdir2)
check("删除不存在的会话返回 False", ok2 is False)

shutil.rmtree(tmpdir)
shutil.rmtree(tmpdir2)

# =============================================================================
# 结果
# =============================================================================
print("\n" + "━" * 64)
total = PASS + FAIL
print(f"结果：{PASS} / {total} 通过" + (", " + str(FAIL) + " 失败" if FAIL else ""))
print("━" * 64)
if FAIL:
    sys.exit(1)
