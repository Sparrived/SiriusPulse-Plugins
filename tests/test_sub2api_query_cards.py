from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sub2api_monitor.visual import (
    build_change_card_html,
    build_rates_html,
    build_subscriptions_html,
)  # noqa: E402


def test_query_cards_show_safe_decision_data_in_ukiyoe_layout() -> None:
    subscription_html = build_subscriptions_html(
        source_id="molly",
        display_name="茉莉 API",
        generated_at=1_700_000_000,
        records=[
            {
                "id": "plan-01",
                "name": "春潮套餐",
                "group_name": "pro",
                "price": 12.5,
                "currency": "CNY",
                "billing_cycle": "monthly",
                "quota": "500 万 token",
                "stock": 7,
                "expires_at": "2026-12-31",
                "enabled": False,
                "password": "must-not-render",
            }
        ],
    )
    rate_html = build_rates_html(
        source_id="molly",
        display_name="茉莉 API",
        generated_at=1_700_000_000,
        records=[
            {
                "groupId": "pro",
                "name": "青波分组",
                "rate_multiplier": 1.25,
                "inputRatio": 0.5,
                "outputRatio": 2,
                "modelRatio": 1.1,
                "peakRateEnabled": True,
                "peakStart": "20:00",
                "peakEnd": "23:00",
                "quota": "must-not-render",
            }
        ],
    )
    for token in ("编号", "额度", "库存", "到期", "已停用"):
        assert token in subscription_html
    for token in ("输入", "输出", "模型", "峰值", "20:00 至 23:00"):
        assert token in rate_html
    assert "<span>GROUP RATES / 分组倍率</span>" in rate_html
    assert "<span>SUBSCRIPTIONS / 可售订阅</span>" in subscription_html
    for html_text in (subscription_html, rate_html):
        assert "SIRIUS PULSE / SUB2API" not in html_text
        assert 'class="wave"' in html_text
        assert "box-shadow: 6px 6px 0 var(--indigo)" in html_text
        assert "white-space: nowrap" in html_text
        assert "text-overflow: ellipsis" in html_text
        assert "rgba" not in html_text
        assert "linear-gradient" not in html_text
        assert "border-radius" not in html_text
        assert "background: transparent" not in html_text
    assert "must-not-render" not in subscription_html
    assert ">must-not-render<" not in rate_html


def test_change_cards_use_compact_event_headers() -> None:
    rate_html = build_change_card_html(
        source_id="molly",
        display_name="茉莉 API 服务节点",
        event_type="rate_changed",
        before={"id": "pro", "name": "青波分组", "rate_multiplier": 1},
        after={"id": "pro", "name": "青波分组", "rate_multiplier": 1.25},
        occurred_at=1_700_000_000,
    )
    subscription_html = build_change_card_html(
        source_id="molly",
        display_name="茉莉 API 服务节点",
        event_type="subscription_changed",
        before={"id": "plan-1", "name": "春潮套餐", "price": 10},
        after={"id": "plan-1", "name": "春潮套餐", "price": 12.5},
        occurred_at=1_700_000_000,
    )

    assert "<span>RATE CHANGE / 倍率变化</span>" in rate_html
    assert "<span>SUBSCRIPTION CHANGE / 订阅更新</span>" in subscription_html
    for html_text in (rate_html, subscription_html):
        assert "SIRIUS PULSE / SUB2API" not in html_text
        assert 'class="event' not in html_text
        assert "white-space: nowrap" in html_text
        assert "text-overflow: ellipsis" in html_text
        assert "rgba" not in html_text
        assert "linear-gradient" not in html_text
        assert "border-radius" not in html_text
        assert "background: transparent" not in html_text
