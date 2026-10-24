from __future__ import annotations

import base64
import html
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sub2api_monitor.plugin import (  # noqa: E402
    Sub2APIMonitorPlugin,
    _format_iq_records,
    _project_iq_records,
)
from sub2api_monitor.visual import build_iq_html  # noqa: E402

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter  # noqa: E402
from sirius_pulse.plugins.context import MessageContext, PluginDataStore  # noqa: E402
from sirius_pulse.plugins.dispatcher import OutputDispatcher  # noqa: E402
from sirius_pulse.plugins.executor import PluginExecutor  # noqa: E402
from sirius_pulse.plugins.lexer import CommandParser, Lexer, PluginMatcher  # noqa: E402
from sirius_pulse.plugins.loader import PluginLoader  # noqa: E402
from sirius_pulse.plugins.registry import PluginRegistry  # noqa: E402

GROUP = "730001"
USER = "730002"


def _payload() -> dict[str, object]:
    return {
        "models": [
            {
                "id": "deepseek-v4-pro-high",
                "label": "DSV4 Pro High",
                "group": "DSV4 Pro",
                "average": 8.65,
                "count": 128,
                "token": "must-not-render",
            },
            {
                "id": "deepseek-v4-pro-medium",
                "label": "DSV4 Pro Medium",
                "group": "DSV4 Pro",
                "average": 0,
                "count": 0,
            },
            {
                "id": "deepseek-v4-pro-low",
                "label": "DSV4 Pro Low",
                "group": "DSV4 Pro",
                "average": None,
                "count": 17,
            },
            {
                "id": "gpt-6-astra-low",
                "label": "GPT-6 Astra Low",
                "group": "GPT-6 Astra",
                "average": 7.2,
                "count": 43,
                "api_key": "must-not-render",
            },
            {
                "id": "deepseek-v3-flash",
                "label": "DeepSeek V3 Flash",
                "group": "DeepSeek V3",
                "average": 9.8,
                "count": 9,
            },
        ]
    }


@pytest.mark.parametrize(
    ("query", "expected_ids", "expected_models"),
    [
        *[
            (
                query,
                [
                    "deepseek-v4-pro-high",
                    "deepseek-v4-pro-medium",
                    "deepseek-v4-pro-low",
                ],
                {"DSV4 Pro"},
            )
            for query in (
                "dsv4",
                "DSV4 Pro High",
                "deepseek-v4-pro-high",
                "deepseek-v4-pro-hig",
            )
        ],
        ("astra", ["gpt-6-astra-low"], {"GPT-6 Astra"}),
    ],
)
def test_iq_projection_searches_series_label_and_id_without_leaking_fields(
    query: str, expected_ids: list[str], expected_models: set[str]
) -> None:
    records = _project_iq_records(_payload(), query)

    assert {record["model"] for record in records} == expected_models
    expected_labels = {
        str(raw["label"])
        for raw in _payload()["models"]
        if isinstance(raw, dict) and raw["id"] in expected_ids
    }
    assert {record["label"] for record in records} == expected_labels
    assert all(
        set(record) == {"model", "label", "effort", "average", "count"}
        for record in records
    )
    assert "must-not-render" not in str(records)
    assert "deepseek-v4-pro-high" not in str(records), "id 只参与查询，不应下发给渲染层"
    assert {record["model"] for record in records}.isdisjoint({"DeepSeek V3"})


def test_iq_projection_keeps_complete_groups_and_distinguishes_null_from_zero() -> None:
    records = _project_iq_records(_payload(), "deepseek-v4-pro-high")

    # 一个档位直接命中时，整个系列保留，但不混入仅靠模糊匹配的系列。
    assert len(records) == 3
    assert {record["effort"] for record in records} == {"High", "Medium", "Low"}
    assert {record["model"] for record in records} == {"DSV4 Pro"}
    assert records[0]["average"] == 8.65 and records[0]["count"] == 128
    zero = next(record for record in records if record["average"] == 0)
    assert zero["count"] == 0
    assert any(record["average"] is None for record in records)

    models = ("DSV4 Pro", "GPT-6 Astra", "GPT-6 Sol", "GPT-6 Terra", "GPT-6 Luna")
    efforts = ("Low", "Medium", "High", "XHigh", "Max", "Ultra")
    payload = {
        "models": [
            {
                "id": f"{model}-{effort}".casefold().replace(" ", "-"),
                "label": f"{model} {effort}",
                "group": model,
                "average": 9 - index / 10,
                "count": index,
            }
            for index, (model, effort) in enumerate(
                ((model, effort) for model in models for effort in efforts)
            )
        ]
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in _project_iq_records(payload, "gpt"):
        grouped.setdefault(str(record["model"]), []).append(record)
    assert set(grouped) == set(models) - {"DSV4 Pro"}
    assert all(len(rows) == len(efforts) for rows in grouped.values())


def test_iq_card_uses_zero_to_ten_svg_lines_and_escapes_untrusted_text() -> None:
    records = _project_iq_records(_payload(), "deepseek-v4-pro-high")
    html_text = build_iq_html(
        records=records, query="DSV4 Pro", generated_at=1_700_000_000
    )
    text = _format_iq_records(records, query="DSV4 Pro")

    for token in (
        "MODEL RATINGS",
        "0–10 分",
        '<div class="iq-chart"><svg viewBox="0 0 648 292"',
        '<g class="iq-series" data-model="DSV4 Pro">',
        '<path class="iq-series-line" d="M 252,246 340,73" stroke="#4de8d2"/>',
        "#080d18",
        "#4de8d2",
        "#ac9cff",
        "grid-template-columns: repeat(2, minmax(0, 1fr))",
        '<span class="iq-legend-value"><b>8.7</b> /10 · n=145</span>',
        '<svg class="iq-legend-mark" viewBox="0 0 28 12" aria-hidden="true">',
        '<circle cx="252" cy="246" r="4"/>',
        '<circle cx="340" cy="73" r="4"/>',
        '<div class="iq-best">8.7<small> /10</small></div>',
        "DSV4 Pro · medium：0 / 10，n=0",
        "DSV4 Pro · high：8.7 / 10，n=128",
        "折线 = 评分变化，非时间趋势",
    ):
        assert token in html_text
    for tick in (0, 2, 4, 6, 8, 10):
        assert f'<text class="iq-axis-text" x="30" y="{250 - tick * 20}"' in html_text
        assert f">{tick}</text>" in html_text
    for index, axis in enumerate(
        ("OFF", "LOW", "MEDIUM", "HIGH", "XHIGH", "MAX", "ULTRA")
    ):
        assert (
            f'<text class="iq-effort-text" x="{76 + index * 88}" y="273" '
            f'text-anchor="middle">{axis}</text>' in html_text
        )
    assert html_text.count('<g class="iq-series-point"') == 3
    assert "DSV4 Pro · low" not in html_text, "null 评分不画点、不连接、不补 0"
    series = re.search(
        r'<g class="iq-series" data-model="DSV4 Pro">(.*?)</g>\s*</g>', html_text
    )
    assert series is not None
    paths = re.findall(r'<path class="iq-series-line" d="([^"]+)"', series.group(1))
    assert paths == ["M 252,246 340,73"], "相邻有效评分应连线；LOW 无评分不画点"
    assert 'cy="246"' in html_text, "真实的 0 分仍应绘制在 0 分轴上"
    assert "iq-track" not in html_text and "width:86.5%" not in html_text
    assert "108.96" not in html_text and ">IQ<" not in html_text
    assert "平均分: 8.65，样本数: 128" in text
    assert "平均分: 0，样本数: 0" in text
    assert "平均分: —，样本数: 17" in text
    assert "0–10 分" in text
    assert "must-not-render" not in html_text + text
    for forbidden in (
        "average_price_usd",
        "average_minutes",
        "cache_hit_rate",
        "DEEP-SWE",
    ):
        assert forbidden not in html_text + text

    gap_records = [
        {"model": "Gap", "effort": "low", "average": 2, "count": 1},
        {"model": "Gap", "effort": "high", "average": None, "count": 2},
        {"model": "Gap", "effort": "max", "average": 6, "count": 3},
        {"model": "Gap", "effort": "ultra", "average": 8, "count": 4},
    ]
    gap_html = build_iq_html(records=gap_records)
    assert 'd="M 516,126 604,86"' in gap_html, "断点两侧的连续评分应单独成段"
    assert "164,206 340" not in gap_html and "340,126" not in gap_html
    assert 'data-model="Gap"' in gap_html

    symbols_html = build_iq_html(
        records=[
            {"model": "Round", "effort": "high", "average": 10, "count": 1},
            {"model": "Square", "effort": "high", "average": 8, "count": 1},
        ]
    )
    assert symbols_html.count('<g class="iq-series-point"') == 4
    assert '<circle cx="340" cy="46" r="4"/>' in symbols_html
    assert '<rect x="336" y="82" width="8" height="8"/>' in symbols_html
    assert symbols_html.count('<svg class="iq-legend-mark"') == 2

    malicious = '<img src=x onerror="steal()">'
    escaped = build_iq_html(
        records=[
            {
                "model": malicious,
                "label": malicious,
                "effort": "low",
                "average": 5,
                "count": 1,
            }
        ],
        query=malicious,
    )
    assert malicious not in escaped
    assert escaped.count(html.escape(malicious, quote=True)) >= 3
    empty_html = build_iq_html(records=[], query="no-such-model")
    assert "NO RATING MATCHES" in empty_html
    assert "未找到匹配的模型评分记录" in empty_html


class IQChat:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin_path = Path(__file__).resolve().parents[1] / "sub2api_monitor"
        self.definition = PluginLoader(plugin_path.parent).load_definition(plugin_path)
        assert self.definition is not None
        assert self.definition.permissions.developer_only is True
        self.root = root
        self.requests: list[httpx.Request] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.render_calls: list[str] = []
        self.adapter = NapCatAdapter(
            "ws://qq.invalid/unconnected",
            work_path=root,
            config={"allowed_group_ids": [GROUP]},
        )
        self.adapter.call_api = self.capture_send  # type: ignore[method-assign]
        self.registry = PluginRegistry()
        self.registry.register(self.definition)
        self.executor = PluginExecutor(
            self.registry,
            persona_data_path=root,
            adapter=self.adapter,
            # 未指定 run_on_persona：IQ 公开查询本身不需要指定人格。
            config_manager=SimpleNamespace(
                get_enabled=lambda _name: True,
                get_settings=lambda _name: {},
                reload=lambda: None,
            ),
        )
        real_client = httpx.AsyncClient

        def mocked_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(self.upstream)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", mocked_client)
        # PluginLoader 的隔离模块名与直接 import 不同，必须替换实际执行模块。
        loaded_module = sys.modules[self.definition._plugin_class.__module__]
        monkeypatch.setattr(loaded_module, "render_iq_card", self.render)

    async def upstream(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=json.dumps(_payload()).encode("utf-8"))

    async def render(
        self, records: list[dict[str, Any]], *, query: str, artifact_dir: Path, **_: Any
    ) -> str:
        # 单元链路旁听的正是 render_iq_card 的契约；独立验收另跑真实 Chromium。
        self.render_calls.append(build_iq_html(records=records, query=query))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        output = artifact_dir / "sub2api_iq_test.png"
        Image.new("RGB", (680, 300), "#080d18").save(output, format="PNG")
        return str(output)

    async def capture_send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        assert action == "send_group_msg"
        self.sent.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 83012}}

    async def send(
        self, command: str = "/sub2api iq DSV4 Pro", *, developer: bool = True
    ) -> str:
        matched = PluginMatcher().match(
            command, self.definition.commands, self.definition.name, Lexer()
        )
        assert matched is not None and matched.lexed is not None
        cmd = CommandParser().parse(matched.lexed, self.definition)
        responses = await self.executor.execute(
            matched.plugin_name,
            cmd,
            group_id=GROUP,
            user_id=USER,
            caller_is_developer=developer,
            message_context=MessageContext(
                group_id=GROUP,
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
                adapter=self.adapter,
                group_id=GROUP,
                user_id=USER,
            )
            texts.append(output.text)
            message = output.message_group or output.text
            if message:
                await self.adapter.send_group_message(GROUP, message)
        return "\n".join(texts)

    def image_sent(self) -> None:
        assert len(self.sent) == 1
        action, params = self.sent[0]
        assert action == "send_group_msg"
        assert params["group_id"] == int(GROUP)
        assert [segment["type"] for segment in params["message"]] == ["image"]
        reference = params["message"][0]["data"]["file"]
        assert reference.startswith("base64://")
        assert base64.b64decode(
            reference.removeprefix("base64://"), validate=True
        ).startswith(b"\x89PNG\r\n\x1a\n")

    def assert_public_request(self) -> None:
        assert len(self.requests) == 1
        request = self.requests[0]
        assert request.method == "GET"
        assert request.url.path == "/api/model-ratings"
        assert dict(request.url.params.multi_items()) == {
            "view": "public",
            "window": "7d",
        }
        assert request.headers["Accept"] == "application/json"
        assert not request.content
        for header in ("Authorization", "Cookie", "X-API-Key"):
            assert header not in request.headers


@pytest.mark.asyncio
async def test_real_user_command_fetches_public_ratings_and_sends_image_without_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = IQChat(tmp_path, monkeypatch)

    text = await chat.send()

    assert text == "", "已直发图片后不得再发送提示文字"
    chat.assert_public_request()
    chat.image_sent()
    assert len(chat.render_calls) == 1
    assert "DSV4 Pro" in chat.render_calls[0]
    assert "DeepSeek V3" not in chat.render_calls[0]
    assert "must-not-render" not in chat.render_calls[0]


@pytest.mark.asyncio
async def test_non_developer_cannot_query_iq_or_probe_public_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = IQChat(tmp_path, monkeypatch)

    text = await chat.send(developer=False)

    assert text == ""
    assert not chat.requests and not chat.sent and not chat.render_calls


@pytest.mark.asyncio
async def test_iq_render_failure_falls_back_to_average_and_count_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = Sub2APIMonitorPlugin()
    plugin._ctx = SimpleNamespace(
        config={},
        data_store=PluginDataStore(tmp_path, "sub2api_monitor"),
        message=MessageContext(group_id=GROUP),
        adapter=None,
    )

    async def failed_render(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("sub2api_monitor.plugin.render_iq_card", failed_render)
    records = _project_iq_records(_payload(), "deepseek-v4-pro-high")
    response = await plugin._send_iq_image(records, "DSV4 Pro")

    assert response.success is True
    assert "近 7 天，0–10 分" in response.text
    assert "DSV4 Pro High（平均分: 8.65，样本数: 128）" in response.text
    assert "DSV4 Pro Medium（平均分: 0，样本数: 0）" in response.text
    assert "DSV4 Pro Low（平均分: —，样本数: 17）" in response.text
    assert "must-not-render" not in response.text

    empty = await plugin._send_iq_image([], "not-a-model")
    assert empty.success is True
    assert "未找到匹配模型" in empty.text
