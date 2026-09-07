from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sub2api_monitor.plugin import (  # noqa: E402
    Sub2APIMonitorPlugin,
    _format_iq_records,
    _project_iq_records,
)
from sub2api_monitor.visual import build_iq_html  # noqa: E402

from sirius_pulse.plugins.api import PluginResponse  # noqa: E402
from sirius_pulse.plugins.models import ArgNode, CommandAST  # noqa: E402


def _payload() -> dict[str, object]:
    return {
        "points": [
            {
                "model": "gpt-6-astra",
                "effort": "high",
                "iq": 108.96,
                "average_price_usd": 3.031148,
                "average_minutes": 12.57,
                "cache_hit_rate": 0.9475,
                "token": "must-not-render",
            },
            {
                "model": "gpt-6-astra",
                "effort": "low",
                "iq": 100.47,
                "average_price_usd": 2.02954,
                "average_minutes": 8.98,
                "cache_hit_rate": 0.947,
            },
            {
                "model": "other-model",
                "effort": "medium",
                "iq": 88.0,
                "average_price_usd": 1.2,
                "average_minutes": 10.0,
                "cache_hit_rate": 0.8,
            },
        ]
    }


def test_iq_projection_fuzzy_matches_and_exposes_only_requested_fields() -> None:
    records = _project_iq_records(_payload(), "gpt-6-atra")

    assert [record["effort"] for record in records] == ["high", "low"]
    assert set(records[0]) == {
        "model",
        "effort",
        "iq",
        "average_price_usd",
        "average_minutes",
        "cache_hit_rate",
    }
    assert "must-not-render" not in str(records)


def test_iq_projection_keeps_complete_gpt_model_groups() -> None:
    models = (
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    )
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")
    payload = {
        "points": [
            {"model": model, "effort": effort, "iq": 100 - index}
            for index, (model, effort) in enumerate(
                ((model, effort) for model in models for effort in efforts)
            )
        ]
    }

    records = _project_iq_records(payload, "gpt")
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(str(record["model"]), []).append(record)

    assert set(grouped) == set(models)
    assert {model: len(rows) for model, rows in grouped.items()} == {
        model: len(efforts) for model in models
    }


def test_iq_card_and_text_fallback_show_requested_fields_only() -> None:
    records = _project_iq_records(_payload(), "astra")
    html_text = build_iq_html(
        records=records, query="astra", generated_at=1_700_000_000
    )
    text = _format_iq_records(records, query="astra")

    for token in ("INTELLIGENCE IQ / 智能效率", "EFFORT", "均价", "耗时", "缓存", "IQ"):
        assert token in html_text
    assert html_text.count("gpt-6-astra") == 1
    assert html_text.index("EFFORT / low") < html_text.index("EFFORT / high")
    assert "iq-model" in html_text
    full_html = build_iq_html(
        records=_project_iq_records(_payload(), ""), generated_at=1_700_000_000
    )
    assert full_html.count('<section class="iq-model">') == 2
    assert full_html.count("gpt-6-astra") == 1
    assert full_html.count("other-model") == 1
    for forbidden in ("must-not-render", "rgba", "linear-gradient", "border-radius"):
        assert forbidden not in html_text
    assert "gpt-6-astra" in text
    assert "· gpt-6-astra" in text
    assert "  - high" in text
    assert "缓存:" in text
    empty_html = build_iq_html(
        records=[], query="no-such-model", generated_at=1_700_000_000
    )
    assert "NO IQ MATCHES" in empty_html
    assert "未找到匹配的模型效率记录" in empty_html


@pytest.mark.asyncio
async def test_iq_command_accepts_multiword_query_without_designated_persona() -> None:
    plugin = Sub2APIMonitorPlugin()
    calls: list[tuple[str, object]] = []

    async def fetch(query: str) -> list[dict[str, object]]:
        calls.append(("fetch", query))
        return [{"model": "gpt-6-astra", "effort": "high", "iq": 108.96}]

    async def send(records: list[dict[str, object]], query: str) -> PluginResponse:
        calls.append(("send", (records, query)))
        return PluginResponse.ok(text="IQ 卡片已生成")

    plugin._fetch_iq_records = fetch  # type: ignore[method-assign]
    plugin._send_iq_image = send  # type: ignore[method-assign]
    cmd = CommandAST(
        command="sub2api",
        raw_text="/sub2api iq gpt 6",
        args=[
            ArgNode(value="iq", raw="iq"),
            ArgNode(value="gpt", raw="gpt"),
            ArgNode(value=6, raw="6"),
        ],
    )

    response = (await plugin.execute_async(cmd))[0]

    assert response.success is True
    assert calls[0] == ("fetch", "gpt 6")
    assert calls[1][0] == "send"
