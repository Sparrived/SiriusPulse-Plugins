"""今日一画能复用的已存形象：群里那些 AI 伙伴的设定图（``{persona}/bot_assets/<key>/``）。

框架约定伙伴角色的三视图 / 立绘放在人格目录的 ``bot_assets/`` 下，由 ``index.json`` 给出名字和外观。
只挑这一期真的在群里出现过（说过话或被 @）的伙伴，把她们的图按顺序接在她自己的参考图后面。
真人群友不在这里：他们的形象要本人同意才能画，所以画里只用看不清长相的 Q 版小人代表。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BOT_ASSETS_DIR_NAME = "bot_assets"
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_PREFERRED = ("turnaround", "standing", "full", "portrait", "avatar")
_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Partner:
    name: str
    path: Path


def _root(engine: Any) -> Path | None:
    assets_dir = getattr(getattr(engine, "brain", None), "persona_assets_dir", None)
    if assets_dir:
        return Path(assets_dir).expanduser().parent / BOT_ASSETS_DIR_NAME
    work_path = getattr(engine, "work_path", None)
    return Path(work_path) / BOT_ASSETS_DIR_NAME if work_path else None


def _picture(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    files = [
        f for f in sorted(folder.iterdir()) if f.is_file() and f.suffix.lower() in _IMAGE_SUFFIXES
    ]
    for token in _PREFERRED:
        picked = next((f for f in files if token in f.name.lower()), None)
        if picked:
            return picked
    return files[0] if files else None


def _matches(label: str, seen: Iterable[str]) -> bool:
    label = label.strip().lower()
    if len(label) < 2:
        return False
    return any(label in name.lower() or name.lower() in label for name in seen if len(name) >= 2)


def present_partners(engine: Any, seen_names: Iterable[str], *, limit: int = 3) -> list[Partner]:
    """这一期出现过、且存了形象图的伙伴。"""
    root = _root(engine)
    if root is None or not root.is_dir():
        return []
    try:
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(index, dict):
        return []
    seen = [str(name) for name in seen_names if name]
    found: list[Partner] = []
    for key in sorted(index):
        item = index.get(key)
        if not isinstance(item, dict) or item.get("hidden"):
            continue
        name = str(item.get("name") or key).strip()
        if not (_matches(name, seen) or _matches(str(key), seen)):
            continue
        picture = _picture(root / key)
        if picture is None:
            continue
        found.append(Partner(name=name, path=picture))
        if len(found) >= limit:
            break
    return found


def read_references(partners: Iterable[Partner]) -> list[tuple[str, tuple[str, bytes]]]:
    """读出伙伴的图：返回 (名字, (文件名, 字节))，读不到的伙伴整个跳过，名字和图始终一一对应。"""
    loaded: list[tuple[str, tuple[str, bytes]]] = []
    for partner in partners:
        try:
            if partner.path.stat().st_size > _MAX_BYTES:
                continue
            loaded.append((partner.name, (partner.path.name, partner.path.read_bytes())))
        except OSError as exc:
            logger.debug("读取伙伴形象图失败 [%s]: %s", partner.name, exc)
    return loaded
