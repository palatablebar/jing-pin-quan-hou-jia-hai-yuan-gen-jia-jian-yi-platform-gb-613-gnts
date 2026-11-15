"""订单接口单测：内存 SQLite + FastAPI TestClient（不依赖 PostgreSQL/Redis）。

覆盖：整单优惠只记一次并按成交金额分摊、退款按业务时间回放、对账闸门拦截
（对账不通过的订单不进跟价样本）、角色权限、重复自然键 409、非法参数 422。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

import pytest

# 必须在导入 src.config.* 之前指定：settings 是模块级单例，engine 在导入时就建好
os.environ["DB_KIND"] = "sqlite"
os.environ["DB_NAME"] = tempfile.mktemp(suffix=".db")
os.environ["SEED_DEMO"] = "false"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.database import SessionLocal
from src.utils.security import create_access_token


@pytest.fixture(scope="module")
def client():
    """整个模块共用一套临时 SQLite 文件（路径在导入前随机生成，跑完即弃）。"""
    from fastapi.testclient import TestClient

    from src.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module", autouse=True)
def seeded(client):
    from scripts.demo_config import build_dataset
    from scripts.seed_demo import (
        ensure_competitors,
        ensure_rules,
        ensure_skus,
        ensure_users,
    )

    dataset = build_dataset()
    with SessionLocal() as db:
        ensure_users(db)
        competitors = ensure_competitors(db, dataset["competitors"])
        skus = ensure_skus(db, dataset["skus"], competitors)
        ensure_rules(db, dataset["rules"], skus)
        db.commit()
    return dataset


def headers(role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(f'{role}@pricewatch.io', role)[0]}"}


def competitor_id(code: str = "dy-muyan") -> int:
    from sqlalchemy import select

    from src.models.competitor import Competitor

    with SessionLocal() as db:
        return db.scalar(select(Competitor).where(Competitor.code == code)).id


def make_order(client, order_no: str, *, payable_declared=None, refunds=None, discounts=None):
    body = {
        "order_no": order_no,
        "competitor_id": competitor_id(),
        "paid_on": "2026-11-10T10:00:00",
        "shipping_fee": 0,
        "items": [
            {"sku_code": "PB-sofa-001", "qty": 2, "unit_price": 600.0},
            {"sku_code": "PB-decor-115", "qty": 1, "unit_price": 300.0},
        ],
        "discounts": discounts
        if discounts is not None
        else [{"seq": 1, "kind": "platform_cross_store", "label": "跨店每满300减30 ×2", "amount": 150.0}],
        "refunds": refunds or [],
    }
    if payable_declared is not None:
        body["payable_declared"] = payable_declared
    return client.post("/api/orders", json=body, headers=headers("admin"))


def test_order_level_discount_allocated_once(client):
    response = make_order(client, "T-ALLOC-001")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["items_total"] == 1500.0
    assert data["discount_total"] == 150.0
    assert data["payable_declared"] == 1350.0
    assert data["reconcile_state"] == "passed"
    allocated = {row["sku_code"]: row["discount_allocated"] for row in data["items"]}
    # 成交金额 1200 : 300 → 分摊 120 : 30；而不是每行都抵 150
    assert allocated == {"PB-sofa-001": 120.0, "PB-decor-115": 30.0}
    assert sum(allocated.values()) == data["discount_total"]
    unit = {row["sku_code"]: row["effective_unit_price"] for row in data["items"]}
    assert unit == {"PB-sofa-001": 540.0, "PB-decor-115": 270.0}
    # 旧口径偏差：每行都被整单 150 全额抵扣，逐行加总 1050+150=1200，比实付 1350 少 150
    assert data["legacy_gap"] == -150.0


def test_duplicate_order_no_conflicts(client):
    assert make_order(client, "T-ALLOC-001").status_code == 409


def test_payable_mismatch_blocks_comparability(client):
    response = make_order(client, "T-FAIL-001", payable_declared=1200.0)
    assert response.status_code == 201
    data = response.json()
    assert data["reconcile_state"] == "failed"
    assert data["reconcile_label"] == "对账不通过"
    failed = [check["key"] for check in data["reconcile_checks"] if not check["passed"]]
    assert "payable_declared" in failed


def test_late_refund_replayed_by_business_time(client):
    response = make_order(
        client,
        "T-LATE-001",
        refunds=[
            # 到达顺序 1，但业务时间更晚（整单全退）
            {"event_no": "LATE", "kind": "order", "occurred_at": "2026-11-12T18:00:00", "amount_declared": 810.0},
            # 到达顺序 2，但业务时间更早（先退 1 件沙发）
            {"event_no": "EARLY", "kind": "item", "sku_code": "PB-sofa-001", "qty": 1, "occurred_at": "2026-11-11T09:00:00"},
        ],
    )
    assert response.status_code == 201, response.text
    data = response.json()
    events = {row["event_no"]: row for row in data["refund_events"]}
    assert events["EARLY"]["business_seq"] == 1
    assert events["LATE"]["business_seq"] == 2
    assert events["EARLY"]["arrival_seq"] == 2
    assert events["EARLY"]["late_arrival"] is True
    assert events["EARLY"]["amount_reversed"] == 540.0  # 沙发单件净额
    assert events["LATE"]["amount_reversed"] == 810.0  # 剩余 1 沙发 + 1 装饰
    assert data["refunded_total"] == 1350.0
    assert data["status"] == "refunded"
    assert data["reconcile_state"] == "passed"


def test_append_refund_is_replayed_immediately(client):
    created = make_order(client, "T-APPEND-001").json()
    response = client.post(
        f"/api/orders/{created['id']}/refunds",
        json={"kind": "item", "sku_code": "PB-decor-115", "qty": 1, "occurred_at": "2026-11-11T12:00:00", "amount_declared": 270.0},
        headers=headers("admin"),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["refunded_total"] == 270.0
    assert data["payable_after_refund"] == 1080.0
    assert data["status"] == "partially_refunded"


def test_over_refund_is_rejected(client):
    created = make_order(client, "T-OVER-001").json()
    response = client.post(
        f"/api/orders/{created['id']}/refunds",
        json={"kind": "item", "sku_code": "PB-decor-115", "qty": 9, "occurred_at": "2026-11-11T12:00:00"},
        headers=headers("admin"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION"


def test_role_gates(client):
    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/orders", headers=headers("viewer")).status_code == 200
    assert client.get("/api/orders", headers=headers("analyst")).status_code == 200
    body = {
        "order_no": "T-ROLE-001",
        "competitor_id": competitor_id(),
        "items": [{"sku_code": "PB-sofa-001", "qty": 1, "unit_price": 100.0}],
    }
    assert client.post("/api/orders", json=body, headers=headers("viewer")).status_code == 403
    assert client.post("/api/orders", json=body, headers=headers("analyst")).status_code == 403
    assert client.post("/api/orders", json=body, headers=headers("admin")).status_code == 201


def test_reconcile_gate_controls_advice_samples(client):
    from sqlalchemy import select

    from src.models.sku import Sku
    from src.services import order_service

    today = date(2026, 11, 12)
    with SessionLocal() as db:
        sku = db.scalar(select(Sku).where(Sku.sku_code == "PB-sofa-001"))
        samples = order_service.comparable_samples(db, sku, as_of=today, days=3)
    order_nos = {sample["order_no"] for sample in samples}
    assert "T-ALLOC-001" in order_nos
    # 对账不通过的订单不进样本池
    assert "T-FAIL-001" not in order_nos
    # 全额退款后剩余件数为 0，也不出样本
    assert "T-LATE-001" not in order_nos


def test_orders_csv_exports(client):
    for path in ("/api/exports/allocations.csv", "/api/exports/refunds.csv", "/api/exports/reconciliations.csv"):
        response = client.get(path, headers=headers("admin"))
        assert response.status_code == 200, path
        assert response.content.startswith("﻿".encode())
    assert client.get("/api/exports/allocations.csv", headers=headers("analyst")).status_code == 403
