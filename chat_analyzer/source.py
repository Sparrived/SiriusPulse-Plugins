"""群聊原料：从引擎归档里按时间窗读出消息，并把每条消息认成「她看得懂」的样子。

归档 ``{work_path}/archive/{group_id}.jsonl`` 是追加写的全量历史，单行还可能带
提示词快照，所以这里从文件尾部按块倒着读，读到窗口之前足够远就停，不整份载入。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

CN_TZ = timezone(timedelta(hours=8))

#: 倒着读时，连续多少条早于窗口的记录之后停下（归档基本按时间追加，留点乱序余量）。
_STOP_AFTER_OLDER = 400
#: 单次最多解析多少行：一周的热闹群也够用，防止病态大文件拖垮进程。
DEFAULT_MAX_LINES = 150_000

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_STICKER = "sticker"
KIND_COMMAND = "command"
KIND_SHARE = "share"

_STICKER_RE = re.compile(
    r"^[\[【]\s*(?:动画表情|表情包|表情|超级表情|商城表情)\s*[:：]|\[CQ:(?:face|mface|marketface)",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(r"^[\[【]\s*(?:图片|闪照|截图)\s*[:：]|\[CQ:image", re.IGNORECASE)
_SHARE_RE = re.compile(r"^[\[【]\s*(?:网易云|QQ音乐|分享|链接|小程序|卡片|视频|语音|文件)\s*[\]】:：]")
#: 正文里夹带的占位标记：[图片: xx.jpg]、【动画表情：xx】、[CQ:...]
_MARKER_RE = re.compile(r"[\[【][^\]】]{0,12}?[:：][^\]】]*[\]】]|\[CQ:[^\]]*\]")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@([^\s@:：,，。!！?？()（）]{1,24})")


@dataclass(slots=True)
class Msg:
    """一条归一化后的群消息。"""

    ts: int
    uid: str
    name: str
    raw: str
    text: str  # 去掉占位标记、链接、@ 之后的正文
    kind: str
    is_bot: bool = False
    mentions: tuple[str, ...] = field(default_factory=tuple)  # 被 @ 的人（uid）

    @property
    def local(self) -> datetime:
        return datetime.fromtimestamp(self.ts, CN_TZ)

    @property
    def hour(self) -> int:
        return self.local.hour

    @property
    def is_media(self) -> bool:
        return self.kind in (KIND_IMAGE, KIND_STICKER)

    @property
    def is_chat_text(self) -> bool:
        """能拿来算词、算笑点、算复读的正常发言。"""
        return self.kind == KIND_TEXT and bool(self.text)


def parse_ts(value: Any) -> int:
    """ISO 8601 → unix 秒；不带时区的按中国时间理解，坏值返回 0。"""
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return int(dt.timestamp())


def classify(raw: str, multimodal: Iterable[Any] = ()) -> tuple[str, str]:
    """认出消息是文字、图片、表情、指令还是分享，并给出去掉标记后的正文。"""
    content = (raw or "").strip()
    if _STICKER_RE.search(content):
        return KIND_STICKER, ""
    if _IMAGE_RE.search(content):
        return KIND_IMAGE, _clean(content)
    if _SHARE_RE.search(content):
        return KIND_SHARE, ""
    if content[:1] in ("/", "#", "！", "!") and len(content) > 1 and not content[1:2].isspace():
        return KIND_COMMAND, ""
    text = _clean(content)
    has_image = any(
        isinstance(item, dict) and str(item.get("type", "")).lower() == "image"
        for item in multimodal or ()
    )
    if has_image and not text:
        return KIND_IMAGE, ""
    return KIND_TEXT, text


def _clean(content: str) -> str:
    text = _MARKER_RE.sub(" ", content)
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _iter_lines_backwards(path: Path, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """从文件尾部开始，一行一行往前吐（不含换行）。"""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            step = min(chunk_size, position)
            position -= step
            handle.seek(position)
            block = handle.read(step) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def read_archive_window(
    path: Path, start_ts: int, end_ts: int, *, max_lines: int = DEFAULT_MAX_LINES
) -> list[dict[str, Any]]:
    """读出落在 [start_ts, end_ts] 里的归档条目（按时间正序）。阻塞 IO，放线程里跑。"""
    picked: list[dict[str, Any]] = []
    older_streak = 0
    for parsed, line in enumerate(_iter_lines_backwards(path)):
        if parsed >= max_lines:
            break
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        ts = parse_ts(entry.get("timestamp"))
        if not ts:
            continue
        if ts < start_ts:
            older_streak += 1
            if older_streak >= _STOP_AFTER_OLDER:
                break
            continue
        older_streak = 0
        if ts <= end_ts:
            entry["_ts"] = ts
            picked.append(entry)
    picked.sort(key=lambda item: item["_ts"])
    return picked


def archive_path(work_path: Path | str | None, group_id: str) -> Path | None:
    """归档文件路径；群号被框架清洗过时按清洗后的名字兜底。"""
    if not work_path:
        return None
    archive_dir = Path(work_path) / "archive"
    direct = archive_dir / f"{group_id}.jsonl"
    if direct.is_file():
        return direct
    safe = re.sub(r"[^0-9A-Za-z_\-一-鿿]+", "_", group_id).strip("_")
    candidate = archive_dir / f"{safe or 'default'}.jsonl"
    return candidate if candidate.is_file() else None


def list_archived_groups(work_path: Path | str | None) -> list[str]:
    if not work_path:
        return []
    archive_dir = Path(work_path) / "archive"
    if not archive_dir.is_dir():
        return []
    groups: list[str] = []
    for path in sorted(archive_dir.glob("*.jsonl")):
        try:
            if path.stat().st_size > 0:
                groups.append(path.stem)
        except OSError:
            continue
    return groups


def _field(entry: Any, key: str, default: Any = "") -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def normalize(entries: Iterable[Any], *, self_uid: str = "") -> list[Msg]:
    """归档条目（dict 或 BasicMemoryEntry）→ :class:`Msg`，丢掉系统消息与坏数据。"""
    raw_messages: list[tuple[Msg, str]] = []
    for entry in entries:
        role = str(_field(entry, "role", "") or "")
        if role == "system":
            continue
        ts = int(_field(entry, "_ts", 0) or 0) or parse_ts(_field(entry, "timestamp"))
        if not ts:
            continue
        uid = str(_field(entry, "channel_user_id", "") or _field(entry, "user_id", "") or "")
        if not uid:
            continue
        sender_type = str(_field(entry, "sender_type", "") or "")
        is_bot = (
            role == "assistant" or sender_type == "self_ai" or (bool(self_uid) and uid == self_uid)
        )
        raw = str(_field(entry, "content", "") or "")
        kind, text = classify(raw, _field(entry, "multimodal_inputs", []) or [])
        name = str(_field(entry, "speaker_name", "") or "").strip()
        raw_messages.append((Msg(ts, uid, name, raw, text, kind, is_bot), raw))

    # @ 只认识群里真的说过话的人：按名字最长匹配到 uid
    names: dict[str, str] = {}
    for msg, _raw in raw_messages:
        if msg.name and not msg.is_bot:
            names.setdefault(msg.name, msg.uid)
    ordered_names = sorted(names, key=len, reverse=True)
    for msg, raw in raw_messages:
        if "@" not in raw or not ordered_names:
            continue
        hits: list[str] = []
        for match in re.finditer(r"@", raw):
            tail = raw[match.end() : match.end() + 26]
            for name in ordered_names:
                if tail.startswith(name):
                    target = names[name]
                    if target != msg.uid and target not in hits:
                        hits.append(target)
                    break
        msg.mentions = tuple(hits)

    messages = [msg for msg, _raw in raw_messages]
    messages.sort(key=lambda item: item.ts)
    return messages
