"""群聊业务验收：真实插件管线、内存 AMKR、真实 PNG 与 OneBot 发送边界。"""

from __future__ import annotations

import base64
import io
import json
import logging
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image, ImageDraw

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.plugins.context import MessageContext
from sirius_pulse.plugins.dispatcher import OutputDispatcher
from sirius_pulse.plugins.executor import PluginExecutor
from sirius_pulse.plugins.lexer import CommandParser, Lexer, PluginMatcher
from sirius_pulse.plugins.loader import PluginLoader
from sirius_pulse.plugins.registry import PluginRegistry

ADMIN_KEY = "acceptance-admin-key-not-for-inference"
RAW_ERROR = "UPSTREAM_PRIVATE_ERROR_83012"
EMAIL = "alice.private@example.invalid"
GROUP = "730001"
USER = "730002"
STAMP = "2026-10-15T02:00:00Z"


def window(label="5 小时", remaining=0.65, **values):
    return {
        "label": label,
        "remaining": remaining,
        "reset_at": "2026-10-15T03:00:00Z",
        "source": "quota",
        **values,
    }


def account(index, name, **values):
    return {
        "auth_index": index,
        "name": name,
        "provider": "OpenAI",
        "plan": "Plus",
        "status": "active",
        "success": 12,
        "failed": 0,
        "windows": [window()],
        **values,
    }


def instance(identifier, accounts, **values):
    return {
        "id": identifier,
        "label": identifier,
        "ok": True,
        "observed_at": STAMP,
        "accounts": accounts,
        **values,
    }


def status_row(provider, requests, successes, *, ttft_ms=900, cache_rate=0.3):
    return {
        "provider_id": provider,
        "cells": [
            {
                "requests": requests,
                "successes": successes,
                "failures": requests - successes,
                "prompt_tokens": requests * 1000,
                "cached_tokens": int(requests * 1000 * cache_rate),
                "total_first_token_ms": requests * ttft_ms,
            }
        ],
    }


def status_payload(points=None, rows=None, rows_truncated=0):
    if points is None:
        points = [
            {
                "started_at": "2026-10-17T10:00:00+08:00",
                "requests": 12,
                "failures": 1,
                "status_codes": {"200": 11, "429": 1},
            },
            {
                "started_at": "2026-10-17T10:10:00+08:00",
                "requests": 8,
                "failures": 0,
                "status_codes": {"200": 8},
            },
            {
                "started_at": "2026-10-17T10:20:00+08:00",
                "requests": 6,
                "failures": 1,
                "status_codes": {"200": 5, "502": 1},
            },
        ]
    if rows is None:
        rows = [
            status_row("openai", 12, 11),
            status_row("azure-east", 9, 9),
            status_row("gemini", 5, 4),
        ]
    return {
        "count_semantics": "upstream_attempt",
        "window": {
            "from": "2026-10-17T10:00:00+08:00",
            "to": "2026-10-18T10:00:00+08:00",
            "hours": 24,
        },
        "filters": {"provider_ids": [], "access_key_ids": []},
        "group_by": "provider",
        "bucket_seconds": 600,
        "snapshot": {
            "total": {
                "requests": 26,
                "successes": 24,
                "failures": 2,
                "prompt_tokens": 26000,
                "cached_tokens": 7800,
                "total_first_token_ms": 26000,
            },
            "current_rpm": 2.5,
        },
        "series": {"bucket_seconds": 600, "points": points},
        "matrix": {"group_by": "provider", "rows": rows, "rows_truncated": rows_truncated},
    }


def snapshot(accounts=None):
    if accounts is not None:
        instances = [instance("cpa-main", accounts)]
        subscriptions = []
    else:
        instances = [
            instance("cpa-main", [account(0, "主力账号"), account(1, "同名账号")]),
            # 另一个账号的名称碰巧等于完整 ID，精确 ID 查询仍应唯一。
            instance(
                "cpa-backup",
                [account(0, "同名账号"), account(1, "Team Two"), account(2, "cpa-main/0")],
            ),
        ]
        subscriptions = [
            {
                "id": "sub-month",
                "name": "月订阅",
                "ok": True,
                "status": "active",
                "observed_at": STAMP,
                "windows": [window("月额度", 0.4)],
                "summary": [{"label": "余额", "value": 23.5, "currency": "USD"}],
            }
        ]
    return {"fetched_at": STAMP, "instances": instances, "subscriptions": subscriptions}


@pytest.fixture(scope="module")
def definition():
    path = Path(__file__).resolve().parents[1] / "amkr_accounts"
    loaded = PluginLoader(path.parent).load_definition(path)
    assert loaded is not None, "真实 PluginLoader 未能加载新插件"
    return loaded


class Chat:
    def __init__(self, definition, data_path):
        self.definition = definition
        self.payload = snapshot()
        self.status_payload = status_payload()
        self.reply = None
        self.requests = []
        self.calls = []
        self.drawn = []
        self.drawn_boxes = []
        self.pngs = []
        self.text = ""
        self.llm = AsyncMock(side_effect=AssertionError("账号资源图片不得交给 LLM"))
        self.adapter = NapCatAdapter(
            "ws://qq.invalid/unconnected",
            work_path=data_path,
            config={"allowed_group_ids": [GROUP]},
        )
        self.adapter.call_api = self.capture_send
        registry = PluginRegistry()
        registry.register(definition)
        self.executor = PluginExecutor(registry, persona_data_path=data_path, adapter=self.adapter)

    async def upstream(self, request):
        self.requests.append(request)
        if self.reply is not None:
            return self.reply(request)
        payload = (
            self.status_payload if request.url.path == "/ui/status-monitor.json" else self.payload
        )
        return httpx.Response(200, content=json.dumps(payload, allow_nan=True).encode())

    async def capture_send(self, action, params):
        assert action in {"send_group_msg", "send_private_msg"}
        self.calls.append((action, params))
        for segment in params["message"]:
            if segment["type"] == "image":
                assert set(segment["data"]) == {"file"}
                reference = segment["data"]["file"]
                assert reference.startswith("base64://")
                png = base64.b64decode(reference.removeprefix("base64://"), validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    assert image.format == "PNG"
                    image.load()
                    assert image.width == 1600
                    assert 300 <= image.height <= 20_000
                    assert image.width * image.height <= 32_000_000
                    for text, (left, top, right, bottom) in self.drawn_boxes:
                        assert 0 <= left <= right <= image.width, f"图中文字横向越界：{text}"
                        assert 0 <= top <= bottom <= image.height, f"图中文字纵向越界：{text}"
                self.pngs.append(png)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 83012}}

    async def send(self, command="/amkr account", *, developer=True, group_id=GROUP):
        self.calls.clear()
        self.drawn.clear()
        self.drawn_boxes.clear()
        self.pngs.clear()
        before = len(self.requests)
        matched = PluginMatcher().match(
            command, self.definition.commands, self.definition.name, Lexer()
        )
        assert matched is not None and matched.lexed is not None
        cmd = CommandParser().parse(matched.lexed, self.definition)
        responses = await self.executor.execute(
            matched.plugin_name,
            cmd,
            group_id=group_id,
            user_id=USER,
            caller_is_developer=developer,
            message_context=MessageContext(
                group_id=group_id,
                user_id=USER,
                channel="qq",
                channel_user_id=USER,
                message_id="83011",
                content=command,
            ),
        )
        texts = []
        for response in responses:
            output = await OutputDispatcher().dispatch(
                response,
                self.definition,
                engine=SimpleNamespace(brain=SimpleNamespace(generate_text=self.llm)),
                adapter=self.adapter,
                group_id=group_id,
                user_id=USER,
            )
            texts.append(output.text)
            message = output.message_group or output.text
            if message:
                if group_id and not group_id.startswith("private_"):
                    await self.adapter.send_group_message(group_id, message)
                else:
                    await self.adapter.send_private_message(USER, message)
        self.text = "\n".join(texts)
        self.llm.assert_not_awaited()
        sent_requests = self.requests[before:]
        assert len(sent_requests) <= 1, "一次聊天查询不得重试/扇出请求或进入推理接口"
        for request in sent_requests:
            assert request.method == "GET"
            assert request.url.path in {"/api/cpa-accounts", "/ui/status-monitor.json"}
            assert request.headers["Authorization"] == f"Bearer {ADMIN_KEY}"
            assert not request.content
        return len(sent_requests)

    def image_sent(self):
        assert len(self.calls) == len(self.pngs) == 1
        action, params = self.calls[0]
        assert action == "send_group_msg"
        assert set(params) == {"group_id", "message"}
        assert params["group_id"] == int(GROUP)
        assert [segment["type"] for segment in params["message"]] == ["image"]
        visible = "\n".join([self.text, *self.drawn]).casefold()
        for pagination in ("page", "翻页", "页码"):
            assert pagination not in visible, f"一次查询应完整输出，不应出现翻页提示：{pagination}"

    def safe(self, logs=""):
        visible = "\n".join([self.text, *self.drawn, logs])
        for secret in (
            ADMIN_KEY,
            RAW_ERROR,
            EMAIL,
            "sk-private-token",
            "amkr_ws_private",
        ):
            assert secret not in visible, f"凭据/账号/上游错误泄漏：{secret}"


@pytest.fixture
def chat(monkeypatch, definition, caplog):
    caplog.set_level(logging.DEBUG)
    # 所有文件只在系统临时目录；退出 fixture 即清理，不触碰项目数据或 pytest cache。
    with tempfile.TemporaryDirectory(prefix="sirius-amkr-acceptance-") as folder:
        root = Path(folder)
        (root / "global_config.json").write_text(
            json.dumps(
                {
                    "amkr_base_url": "https://amkr.invalid",
                    "amkr_local_api_key": ADMIN_KEY,
                }
            ),
            encoding="utf-8",
        )
        for name in ("BASE_URL", "API_KEY", "WORKSPACE", "PUBLIC_URL"):
            monkeypatch.delenv(f"SIRIUS_AMKR_{name}", raising=False)
        rig = Chat(definition, root)
        client = httpx.AsyncClient

        def memory_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(rig.upstream)
            return client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", memory_client)
        # 旁听真正画到 PNG 的文字，避免 OCR 依赖；仍执行原版 Pillow 绘制与编码。
        paint = ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            rig.drawn.append(str(text))
            rig.drawn_boxes.append((str(text), draw.textbbox(xy, text, font=kwargs.get("font"))))
            return paint(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
        yield rig
        assert [path.name for path in root.rglob("*")] == [
            "global_config.json"
        ], "只读查询不应保存账号快照、凭据或临时图片"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "visible", "absent", "count"),
    [
        ("/amkr account", "月订阅", None, 6),
        ("/amkr account all", "月订阅", None, 6),
        ("/amkr account cpa-main/0", "主力账号", "同名账号", 1),
        ("/amkr account 主力账号", "主力账号", "同名账号", 1),
        ("/amkr account Team Two", "Team Two", "主力账号", 1),
        ('/amkr account "Team Two"', "Team Two", "主力账号", 1),
        ("/amkr account 'Team Two'", "Team Two", "主力账号", 1),
        ("/amkr account cpa-main", "同名账号", "Team Two", 2),
        ("/amkr account sub-month", "月订阅", "主力账号", 1),
    ],
)
async def test_developer_queries_select_resources_and_send_real_png(
    chat, command, visible, absent, count
):
    assert await chat.send(command) == 1
    chat.image_sent()
    assert visible in chat.drawn
    if absent is not None:
        assert absent not in chat.drawn
    else:
        assert {"主力账号", "同名账号", "Team Two", "cpa-main/0", "月订阅"} <= set(chat.drawn)
        assert chat.drawn.count("同名账号") == 2
        assert "ID sub-month" in chat.drawn
    assert f"{count} 项" in chat.text
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "message"),
    [("没有此账号", "未找到"), ("cpa-main/", "未找到"), ("同名账号", "名称不唯一")],
)
async def test_unknown_or_ambiguous_selector_never_guesses_an_account(chat, selector, message):
    assert await chat.send(f"/amkr account {selector}") == 1
    assert message in chat.text
    assert not chat.pngs
    if selector == "同名账号":
        assert "cpa-main/1" in chat.text and "cpa-backup/0" in chat.text
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/amkr", "/amkr help"])
async def test_help_does_not_offer_pagination(chat, command):
    assert await chat.send(command) == 0
    assert "/amkr account" in chat.text and "/amkr status" in chat.text
    assert not chat.pngs
    assert not any(hint in chat.text.casefold() for hint in ("page", "翻页", "页码"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["/amkr account", "/amkr account all", "/amkr account cpa-main/0"]
)
async def test_seventeen_windows_wrap_inside_one_account_row_without_repeating_identity(
    chat, command
):
    labels = [f"window-{i:02d}" for i in range(1, 18)]
    chat.payload = snapshot(
        [
            account(
                0,
                "多窗口账号",
                windows=[window(label) for label in labels],
                summary=[{"label": "布局余额", "value": 19.5, "currency": "USD"}],
            ),
            account(1, "下一账号", windows=[]),
        ]
    )
    assert await chat.send(command) == 1
    chat.image_sent()
    assert all(chat.drawn.count(label) == 1 for label in labels)
    for identity in ("多窗口账号", "ID cpa-main/0", "布局余额  19.5 USD"):
        assert chat.drawn.count(identity) == 1, f"账号身份/摘要不得按窗口拆分重复：{identity}"
    boxes = dict(chat.drawn_boxes)
    window_boxes = [boxes[label] for label in labels]
    first_row = window_boxes[:4]
    assert len({box[0] for box in first_row}) == 4, "首排应横向排列四个额度窗口"
    assert len({box[1] for box in first_row}) == 1, "首排四个窗口的可见标题应等高"
    assert all(left[2] < right[0] for left, right in zip(first_row, first_row[1:]))
    assert all(box[1] > first_row[0][1] for box in window_boxes[4:])
    assert {box[0] for box in window_boxes} == {box[0] for box in first_row}
    identity_right = max(boxes[text][2] for text in ("多窗口账号", "ID cpa-main/0"))
    # 旁听原版 ImageDraw 后的可见 bbox；不依赖私有布局函数或固定行高。
    window_content = [
        box
        for text, box in chat.drawn_boxes
        if text in {*labels, "剩余 65%"} or text.startswith("现场查询")
    ]
    assert len(window_content) == len(labels) * 3
    assert all(box[0] > identity_right for box in window_content)
    assert all(box[1] >= boxes["多窗口账号"][1] for box in window_content)
    if command.endswith("cpa-main/0"):
        assert "下一账号" not in chat.drawn
        assert "1 项" in chat.text
    else:
        assert chat.drawn.count("下一账号") == chat.drawn.count("ID cpa-main/1") == 1
        assert max(box[3] for box in window_content) < boxes["下一账号"][1]
        assert boxes["多窗口账号"][0] == boxes["下一账号"][0]
        assert "2 项" in chat.text
    chat.safe()


@pytest.mark.asyncio
async def test_more_than_sixty_four_accounts_requires_narrower_query_without_truncating(chat):
    chat.payload = snapshot([account(index, f"资源-{index:02d}") for index in range(65)])
    assert await chat.send("/amkr account all") == 1
    assert "64" in chat.text and "缩小" in chat.text and "查询" in chat.text
    assert not chat.pngs and not chat.drawn
    assert not any(hint in chat.text.casefold() for hint in ("page", "翻页", "页码"))
    chat.safe()

    assert await chat.send("/amkr account cpa-main/64") == 1
    chat.image_sent()
    assert "资源-64" in chat.drawn
    assert "1 项" in chat.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "windows_per_account", [[128, 128, 1], [128, 128, 128]], ids=["257-windows", "384-windows"]
)
async def test_many_windows_keep_three_account_identities_in_one_png(chat, windows_per_account):
    labels = [
        [f"w{index}-{slot:03d}" for slot in range(count)]
        for index, count in enumerate(windows_per_account)
    ]
    chat.payload = snapshot(
        [
            account(index, f"资源-{index:02d}", windows=[window(label) for label in account_labels])
            for index, account_labels in enumerate(labels)
        ]
    )
    assert await chat.send("/amkr account all") == 1
    chat.image_sent()
    assert "3 项" in chat.text
    boxes = dict(chat.drawn_boxes)
    for index, account_labels in enumerate(labels):
        name = f"资源-{index:02d}"
        assert chat.drawn.count(name) == chat.drawn.count(f"ID cpa-main/{index}") == 1
        assert all(chat.drawn.count(label) == 1 for label in account_labels)
        assert boxes[name][1] <= min(boxes[label][1] for label in account_labels)
        if index + 1 < len(labels):
            assert (
                max(boxes[label][3] for label in account_labels) < boxes[f"资源-{index + 1:02d}"][1]
            )
    chat.safe()


@pytest.mark.asyncio
async def test_excessive_image_height_requires_narrower_query_before_drawing(chat):
    chat.payload = snapshot(
        [account(index, f"超长资源-{index:02d}", windows=[window()] * 128) for index in range(64)]
    )
    assert await chat.send("/amkr account all") == 1
    assert "缩小" in chat.text and "查询" in chat.text
    assert "20000" in chat.text or "32" in chat.text or "像素" in chat.text or "高度" in chat.text
    assert not chat.pngs and not chat.drawn
    assert not any(hint in chat.text.casefold() for hint in ("page", "翻页", "页码"))
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (401, "认证失败"),
        (403, "认证失败"),
        (404, "升级"),
        (502, "HTTP 502"),
        (302, "HTTP 302"),
        ("timeout", "超时"),
        ("connection", "无法连接"),
        ("not-json", "JSON"),
        ("not-object", "格式错误"),
        ("missing-lists", "格式错误"),
        ("bad-record", "格式错误"),
        ("bad-quota", "额度格式错误"),
    ],
)
async def test_upstream_failures_are_safe_and_never_follow_redirects(
    chat, caplog, failure, message
):
    private = f"{RAW_ERROR} {ADMIN_KEY} {EMAIL} sk-private-token amkr_ws_private"

    def reply(request):
        if isinstance(failure, int):
            return httpx.Response(
                failure,
                text=private,
                headers={"Location": "https://account.invalid/v1/private"},
            )
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "connection":
            raise httpx.ConnectError(private, request=request)
        if failure == "not-json":
            return httpx.Response(200, text=private)
        payload = {
            "not-object": [],
            "missing-lists": {"error": private},
            "bad-record": {"instances": [private], "subscriptions": []},
            "bad-quota": snapshot([account(0, "主力账号", model_quotas={"model": private})]),
        }[failure]
        return httpx.Response(200, json=payload)

    chat.reply = reply
    assert await chat.send() == 1
    assert message in chat.text
    assert not chat.pngs
    chat.safe(caplog.text)


@pytest.mark.asyncio
async def test_partial_instance_and_quota_failures_keep_healthy_resources_visible(chat, caplog):
    chat.payload = snapshot([account(0, "可读账号", quota_error=RAW_ERROR)])
    chat.payload["instances"].append(
        instance("cpa-offline", [], ok=False, error=f"{RAW_ERROR} {EMAIL} {ADMIN_KEY}")
    )
    chat.payload["subscriptions"] = [
        {"id": "offline-sub", "name": "故障订阅", "ok": False, "error": RAW_ERROR},
        {
            "id": "healthy-sub",
            "name": "可读订阅",
            "ok": True,
            "status": "active",
            "model_quotas": {"model-a": {"windows": [window("每周", 0.2)]}},
        },
    ]
    assert await chat.send() == 1
    chat.image_sent()
    painted = "\n".join(chat.drawn)
    assert {"可读账号", "可读订阅", "故障订阅", "cpa-offline"} <= set(chat.drawn)
    assert "额度查询失败" in painted
    assert "资源读取失败" in painted
    assert "剩余 65%" in chat.drawn and "剩余 20%" in chat.drawn
    chat.safe(caplog.text)


@pytest.mark.asyncio
async def test_zero_full_low_and_missing_quota_are_not_conflated(chat):
    chat.payload = snapshot(
        [
            account(
                0,
                "耗尽账号",
                windows=[window(remaining=0, source="passive")],
                server_time_offset_ms=60000,
            ),
            account(1, "满额账号", windows=[window(remaining=1)]),
            account(2, "低额账号", windows=[window(remaining=0.049)]),
            account(3, "未知账号", windows=[]),
        ]
    )
    assert await chat.send() == 1
    chat.image_sent()
    assert {"剩余 0%", "剩余 100%", "剩余 4%"} <= set(chat.drawn)
    assert any("被动快照" in text and "10/15 10:59" in text for text in chat.drawn)
    assert any("现场查询" in text and "10/15 11:00" in text for text in chat.drawn)
    assert "上游未提供可读额度窗口" in chat.drawn
    assert "未知不代表 0%，也不代表无限额度。" in chat.drawn


@pytest.mark.asyncio
async def test_nonfinite_or_invalid_numbers_render_unknown_without_fabricated_percentages(
    chat,
):
    values = [
        float("nan"),
        float("inf"),
        -float("inf"),
        True,
        None,
        "0.4",
        -0.1,
        1.1,
        10**400,
    ]
    chat.payload = snapshot(
        [
            account(
                0,
                "未知额度",
                windows=[window(f"window-{i}", value) for i, value in enumerate(values)],
                success=float("inf"),
                failed=True,
                server_time_offset_ms=float("nan"),
                summary=[{"label": "非法余额", "value": float("nan")}],
            )
        ]
    )
    assert await chat.send() == 1
    chat.image_sent()
    assert chat.drawn.count("未知") == len(values)
    assert not any(text.startswith("剩余 ") for text in chat.drawn)
    assert "非法余额" not in "\n".join(chat.drawn)


@pytest.mark.asyncio
async def test_empty_global_resources_still_send_an_honest_image(chat):
    chat.payload = {"fetched_at": STAMP, "instances": [], "subscriptions": []}
    assert await chat.send() == 1
    chat.image_sent()
    assert "暂无账号资源" in chat.drawn
    assert "0 项" in chat.text


@pytest.mark.asyncio
async def test_all_group_members_can_query_default_and_selected_accounts_with_rate_limit(chat):
    assert chat.definition.permissions.developer_only is False
    assert await chat.send(developer=False) == 1
    chat.image_sent()
    assert "6 项" in chat.text and "月订阅" in chat.drawn
    chat.safe()

    assert await chat.send("/amkr account cpa-main/0", developer=False) == 1
    chat.image_sent()
    assert "1 项" in chat.text and "主力账号" in chat.drawn
    assert "同名账号" not in chat.drawn and "月订阅" not in chat.drawn
    chat.safe()

    assert await chat.send(developer=False) == 0
    assert len(chat.requests) == 2
    assert "调用过于频繁" in chat.text and "每分钟最多 2 次" in chat.text
    assert not chat.pngs and not chat.drawn
    assert len(chat.calls) == 1 and chat.calls[0][0] == "send_group_msg"
    assert [segment["type"] for segment in chat.calls[0][1]["message"]] == ["text"]
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize("developer", [True, False])
@pytest.mark.parametrize("group_id", ["", f"private_{USER}"])
@pytest.mark.parametrize("command", ["/amkr account", "/amkr status"])
async def test_private_messages_never_probe_amkr(chat, developer, group_id, command):
    assert await chat.send(command, developer=developer, group_id=group_id) == 0
    assert not chat.pngs and not chat.drawn
    assert "群聊" in chat.text
    assert len(chat.calls) == 1 and chat.calls[0][0] == "send_private_msg"
    assert chat.calls[0][1]["user_id"] == int(USER)
    chat.safe()


@pytest.mark.asyncio
async def test_credentials_emails_and_untrusted_fields_never_reach_image_or_logs(chat, caplog):
    raw = account(
        0,
        EMAIL,
        provider="sk-private-token",
        plan="amkr_ws_private",
        status=EMAIL,
        password=RAW_ERROR,
        refresh_token=RAW_ERROR,
        api_key=ADMIN_KEY,
        quota_error=f"{RAW_ERROR} {ADMIN_KEY}",
        windows=[window(ADMIN_KEY, group=EMAIL)] + [window(f"slot-{i}") for i in range(16)],
        summary=[{"label": "余额", "value": 12, "unit": EMAIL}],
    )
    chat.payload = snapshot([raw])
    chat.payload["instances"][0]["label"] = ADMIN_KEY
    chat.payload["instances"][0]["error"] = RAW_ERROR
    assert await chat.send() == 1
    chat.image_sent()
    assert "a***@example.invalid" in "\n".join(chat.drawn)
    chat.safe(caplog.text)

    # 同名错误也必须脱敏完整 ID，不能借歧义提示把账号名/凭据返回群里。
    duplicate = deepcopy(raw)
    duplicate["auth_index"] = EMAIL
    raw["auth_index"] = ADMIN_KEY
    chat.payload = snapshot([raw, duplicate])
    assert await chat.send(f"/amkr account {EMAIL}") == 1
    assert "名称不唯一" in chat.text
    assert not chat.pngs
    chat.safe(caplog.text)


# ---------------------------------------------------------------------------
# /amkr status：AMKR 6.9 状态监控（概览 + 趋势 + 渠道健康矩阵）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_dashboard_sends_health_matrix_trend_and_errors(chat):
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    request = chat.requests[-1]
    assert request.url.path == "/ui/status-monitor.json"
    assert dict(request.url.params) == {"hours": "24", "group_by": "provider"}
    assert "近 24 小时" in chat.text and "成功率 92.3%" in chat.text
    for text in (
        "渠道状态 · 健康监控",
        "请求趋势 · 每桶 10 分钟",
        "渠道健康 · 按供应商",
        "错误归因",
        "健康 95",
        "健康 100",
        "健康 88",
        "openai",
        "azure-east",
        "gemini",
        "429 × 1",
        "502 × 1",
        "10/17 10:00",
    ):
        assert text in chat.drawn, f"状态看板缺少可见内容：{text}"
    assert "26 次请求" in "\n".join(chat.drawn)
    chat.safe()


@pytest.mark.asyncio
async def test_status_accepts_hour_window(chat):
    chat.status_payload["window"]["hours"] = 6
    assert await chat.send("/amkr status 6") == 1
    chat.image_sent()
    assert dict(chat.requests[-1].url.params)["hours"] == "6"
    assert "近 6 小时" in chat.text
    assert "近 6 小时" in "\n".join(chat.drawn)
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "/amkr status 0",
        "/amkr status 8761",
        "/amkr status abc",
        "/amkr status 1.5",
        "/amkr status -1",
        "/amkr status 6 7",
    ],
)
async def test_status_rejects_invalid_hours_without_probing_amkr(chat, command):
    assert await chat.send(command) == 0
    assert "小时数" in chat.text and "1–8760" in chat.text
    assert not chat.requests and not chat.pngs and not chat.drawn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (401, "认证失败"),
        (403, "认证失败"),
        (404, "6.9.0"),
        (422, "参数无效"),
        (500, "HTTP 500"),
        ("timeout", "超时"),
        ("connection", "无法连接"),
        ("not-json", "JSON"),
        ("not-object", "格式错误"),
        ("missing-matrix", "格式错误"),
        ("bad-point", "格式错误"),
    ],
)
async def test_status_upstream_failures_are_safe_and_never_leak(chat, caplog, failure, message):
    private = f"{RAW_ERROR} {ADMIN_KEY} {EMAIL} sk-private-token amkr_ws_private"

    def reply(request):
        if isinstance(failure, int):
            return httpx.Response(
                failure,
                text=private,
                headers={"Location": "https://account.invalid/v1/private"},
            )
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "connection":
            raise httpx.ConnectError(private, request=request)
        if failure == "not-json":
            return httpx.Response(200, text=private)
        payload = {
            "not-object": [],
            "missing-matrix": {"snapshot": {"total": {}}, "series": {"points": []}},
            "bad-point": {
                "snapshot": {"total": {}},
                "series": {"points": [private]},
                "matrix": {"rows": []},
            },
        }[failure]
        return httpx.Response(200, json=payload)

    chat.reply = reply
    assert await chat.send("/amkr status") == 1
    assert message in chat.text
    assert not chat.pngs
    chat.safe(caplog.text)


@pytest.mark.asyncio
async def test_status_empty_window_is_honest_and_never_divides_by_zero(chat):
    chat.status_payload = status_payload(points=[], rows=[])
    chat.status_payload["snapshot"] = {
        "total": {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "total_first_token_ms": 0,
        },
        "current_rpm": 0,
    }
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    assert "窗口内没有请求" in chat.text
    assert "窗口内没有请求" in chat.drawn
    chat.safe()


@pytest.mark.asyncio
async def test_status_marks_health_bands_and_discloses_truncated_rows(chat):
    chat.status_payload = status_payload(
        rows=[
            status_row("healthy", 10, 10),
            status_row("watch", 10, 6),
            status_row("broken", 10, 4, ttft_ms=9000, cache_rate=0),
            status_row("few", 2, 2),
            status_row("idle", 0, 0),
        ],
        rows_truncated=2,
    )
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    for text in ("健康 100", "注意 76", "异常 24", "样本不足", "无流量"):
        assert text in chat.drawn, f"健康矩阵缺少语义标签：{text}"
    assert "另有 2 行未显示" in "\n".join(chat.drawn)
    chat.safe()


@pytest.mark.asyncio
async def test_status_provider_labels_are_redacted(chat, caplog):
    chat.status_payload = status_payload(
        rows=[status_row(f"openai-{EMAIL}", 10, 10), status_row(ADMIN_KEY, 5, 5)]
    )
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    visible = "\n".join(chat.drawn)
    assert "***@example.invalid" in visible and EMAIL not in visible
    chat.safe(caplog.text)


@pytest.mark.asyncio
async def test_all_group_members_can_query_status(chat):
    assert chat.definition.permissions.developer_only is False
    assert await chat.send("/amkr status", developer=False) == 1
    chat.image_sent()
    assert "成功率" in chat.text
    chat.safe()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["rows", "points"])
async def test_status_oversized_payload_is_rejected_before_drawing(chat, field):
    if field == "rows":
        chat.status_payload = status_payload(rows=[status_row(f"p{i}", 1, 1) for i in range(61)])
    else:
        chat.status_payload = status_payload(
            points=[
                {"started_at": "2026-10-17T10:00:00+08:00", "requests": 1, "failures": 0}
                for _ in range(501)
            ]
        )
    assert await chat.send("/amkr status") == 1
    assert "安全上限" in chat.text
    assert not chat.pngs and not chat.drawn
    chat.safe()


@pytest.mark.asyncio
async def test_status_unattributed_channel_is_labeled_not_left_blank(chat):
    chat.status_payload = status_payload(rows=[status_row(None, 5, 5)])
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    assert "未归属" in chat.drawn
    chat.safe()


@pytest.mark.asyncio
async def test_status_degrades_honestly_without_rpm_or_error_samples(chat):
    chat.status_payload = status_payload(
        points=[{"started_at": "2026-10-17T10:00:00+08:00", "requests": 5, "failures": 0}],
        rows=[status_row("openai", 5, 5)],
    )
    chat.status_payload["bucket_seconds"] = 86400
    chat.status_payload["snapshot"] = {
        "total": {
            "requests": 5,
            "successes": 5,
            "failures": 0,
            "prompt_tokens": 5000,
            "cached_tokens": 1500,
            "total_first_token_ms": 5000,
        }
    }
    assert await chat.send("/amkr status") == 1
    chat.image_sent()
    visible = "\n".join(chat.drawn)
    assert "当前 RPM —" in visible
    assert "窗口内没有非 2xx 响应" in chat.drawn
    assert "请求趋势 · 每桶 1 天" in chat.drawn
    chat.safe()
