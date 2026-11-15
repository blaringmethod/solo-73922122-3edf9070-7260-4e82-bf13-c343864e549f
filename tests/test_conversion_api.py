"""换算核查 HTTP 测试：上传解析、状态隔离、单版本快照、并发批改不串版本。"""

from __future__ import annotations

import csv
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import BomStore

DETAIL_HEADER = "line_id,leaf_part,unit,qty\n"
RULE_HEADER = "leaf_part,from_unit,to_unit,numerator,denominator\n"


def _client(tmp_path, name):
    store = BomStore(str(tmp_path / "shared.db"))
    app = create_app(store, instance=name, demo_mode=True, seed_demo=False)
    return TestClient(app, base_url=f"http://{name}.local")


def _files(detail: str, rules: str):
    return {
        "detail_csv": ("detail.csv", detail.encode("utf-8"), "text/csv"),
        "rules_csv": ("rules.csv", rules.encode("utf-8"), "text/csv"),
    }


@pytest.fixture()
def client(tmp_path):
    # 已发布 BOM：FG -> SA(2) -> L1(3)；FG -> L2(4)；L1、L2 是叶件，SA/FG 不是。
    with _client(tmp_path, "alpha") as test_client:
        response = test_client.post(
            "/api/batch",
            json={
                "ops": [
                    {"op": "upsert", "parent": "FG", "child": "SA", "qty": 2},
                    {"op": "upsert", "parent": "SA", "child": "L1", "qty": 3},
                    {"op": "upsert", "parent": "FG", "child": "L2", "qty": 4},
                ]
            },
        )
        assert response.status_code == 200
        yield test_client


def _basic_payload():
    detail = (
        DETAIL_HEADER
        + "D1,L1,箱,2\n"       # 2 × 10 × 12 = 240 件，有效
        + "D2,L1,包,3\n"       # 3 × 12 = 36 件，有效
        + "D3,L2,盒,1\n"       # 1/3 件，非整数拒收
        + "D4,SA,箱,1\n"       # SA 是装配件，不是叶件
        + "D5,L9,箱,1\n"       # BOM 中不存在
    )
    rules = (
        RULE_HEADER
        + "L1,箱,包,10,1\n"
        + "L1,包,件,12,1\n"
        + "L2,盒,件,1,3\n"
        + "SA,箱,件,100,1\n"
    )
    return detail, rules


def _expected_basic_counts():
    # D1/D2 有效；D3 非整数、D4 非叶件、D5 不存在 → 3 行拒收。
    return {"valid": 2, "quarantined": 0, "rejected": 3}


def test_check_endpoint_classifies_all_lines(client):
    detail, rules = _basic_payload()
    response = client.post("/api/conversion/check", files=_files(detail, rules))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["bom_version_seq"] >= 1
    assert report["piece_unit"] == "件"
    assert report["counts"] == {"valid": 2, "quarantined": 0, "rejected": 3}
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["standard_qty"] == 240
    assert rows["D1"]["status"] == "valid"
    assert rows["D2"]["standard_qty"] == 36
    assert rows["D3"]["status"] == "rejected" and "不是整数" in rows["D3"]["reason"]
    assert rows["D4"]["status"] == "rejected" and "叶件" in rows["D4"]["reason"]
    assert rows["D5"]["status"] == "rejected" and "叶件" in rows["D5"]["reason"]
    # 有效行记录所据 BOM 版本。
    assert rows["D1"]["bom_version_seq"] == report["bom_version_seq"]


def test_conflicting_part_is_quarantined_while_others_still_produce(client):
    detail = (
        DETAIL_HEADER
        + "D1,L1,箱,1\n"
        + "D2,L1,箱,2\n"
        + "D3,L2,箱,1\n"
    )
    rules = (
        RULE_HEADER
        + "L1,箱,件,120,1\n"
        + "L1,箱,包,10,1\n"
        + "L1,包,件,5,1\n"     # 10×5=50，与直达 120 矛盾
        + "L2,箱,件,8,1\n"
    )
    response = client.post("/api/conversion/check", files=_files(detail, rules))
    assert response.status_code == 200
    report = response.json()
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["status"] == "quarantined"
    assert rows["D2"]["status"] == "quarantined"
    assert rows["D3"]["status"] == "valid" and rows["D3"]["standard_qty"] == 8
    assert report["quarantined_parts"] == ["L1"]
    conflict = report["conflicts"][0]
    assert conflict["leaf_part"] == "L1"
    assert set(conflict["rates"]) == {"120", "50"}
    assert len(conflict["rules"]) == 3


def test_valid_rows_csv_download_is_single_version_snapshot(client):
    detail, rules = _basic_payload()
    response = client.post("/api/conversion/valid-rows.csv", files=_files(detail, rules))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    version = response.headers["X-Bom-Version"]
    assert response.headers["X-Valid-Rows"] == "2"
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {r["line_id"] for r in rows} == {"D1", "D2"}
    assert {r["bom_version_seq"] for r in rows} == {version}
    assert int(next(r for r in rows if r["line_id"] == "D1")["standard_qty"]) == 240


def test_missing_file_and_bad_csv_return_422(client):
    detail, rules = _basic_payload()
    bad = client.post(
        "/api/conversion/check",
        files={"rules_csv": ("r.csv", rules.encode(), "text/csv")},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "ConversionError"

    malformed = client.post(
        "/api/conversion/check",
        files=_files("line_id,leaf_part\nx,L1\n", rules),
    )
    assert malformed.status_code == 422
    assert "缺少必需列" in malformed.json()["error"]


def test_oversized_upload_is_rejected_with_422(client):
    from app.main import MAX_UPLOAD_BYTES

    detail, rules = _basic_payload()
    padding = "x" * (MAX_UPLOAD_BYTES + 64)
    huge = detail + f"D99,L1,箱,{padding}\n"
    response = client.post("/api/conversion/check", files=_files(huge, rules))
    assert response.status_code == 422
    assert "上限" in response.json()["error"]


def test_check_uses_one_committed_version_while_another_process_commits(tmp_path):
    """另一 Web 进程批改期间，核查始终依据同一已提交版本（不串版本）。"""
    with _client(tmp_path, "alpha") as alpha, _client(tmp_path, "beta") as beta:
        setup = alpha.post(
            "/api/batch",
            json={
                "ops": [
                    {"op": "upsert", "parent": "FG", "child": "SA", "qty": 2},
                    {"op": "upsert", "parent": "SA", "child": "L1", "qty": 3},
                    {"op": "upsert", "parent": "FG", "child": "L2", "qty": 4},
                ]
            },
        )
        assert setup.status_code == 200
        version_before = alpha.get("/api/state").json()["version"]["seq"]
        # 全部引用当前 BOM 叶件，保证每行都带版本号、可校验单版本一致性。
        detail = (
            DETAIL_HEADER
            + "D1,L1,箱,2\n"
            + "D2,L1,包,3\n"
            + "D3,L2,箱,1\n"
        )
        rules = (
            RULE_HEADER
            + "L1,箱,包,10,1\n"
            + "L1,包,件,12,1\n"
            + "L2,箱,件,8,1\n"
        )
        seen_versions: list[int] = []
        stop = threading.Event()

        def post_check(client):
            # 每次请求都重建文件句柄：httpx 会在上一次请求后耗尽流。
            return client.post("/api/conversion/check", files=_files(detail, rules))

        def post_csv(client):
            return client.post("/api/conversion/valid-rows.csv", files=_files(detail, rules))

        def writer():
            qty = 5
            while not stop.is_set():
                resp = beta.post(
                    "/api/batch",
                    json={
                        "ops": [
                            {"op": "upsert", "parent": "FG", "child": "SA", "qty": 2},
                            {"op": "upsert", "parent": "SA", "child": "L1", "qty": 3},
                            {"op": "upsert", "parent": "FG", "child": "L2", "qty": qty},
                        ]
                    },
                )
                if resp.status_code == 200:
                    qty += 1
                time.sleep(0.002)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(20):
                report = post_check(alpha).json()
                # 每份报告自身只有一个版本，且必为某个已提交版本。
                seq = report["bom_version_seq"]
                seen_versions.append(seq)
                assert seq >= version_before
                assert {
                    r["bom_version_seq"]
                    for r in report["rows"]
                    if r["bom_version_seq"] is not None
                } == {seq}
                csv_resp = post_csv(alpha)
                download_version = csv_resp.headers["X-Bom-Version"]
                assert download_version.isdigit()
                download_rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
                assert {r["bom_version_seq"] for r in download_rows} == {download_version}
        finally:
            stop.set()
            thread.join()
        # 并发提交确实发生过，版本向前推进，但没有任何一份报告混版本。
        assert max(seen_versions) > version_before


def test_new_commit_changes_leaf_set_seen_by_next_check(tmp_path):
    """上一次核查后 BOM 发布了新版本，下一次核查依据新版本（叶件集合变化）。"""
    with _client(tmp_path, "alpha") as alpha:
        setup = alpha.post(
            "/api/batch",
            json={
                "ops": [
                    {"op": "upsert", "parent": "FG", "child": "SA", "qty": 2},
                    {"op": "upsert", "parent": "SA", "child": "L1", "qty": 3},
                    {"op": "upsert", "parent": "FG", "child": "L2", "qty": 4},
                ]
            },
        )
        assert setup.status_code == 200
        detail, _rules = _basic_payload()
        first = alpha.post(
            "/api/conversion/check", files=_files(detail, _rules)
        ).json()
        assert first["counts"] == _expected_basic_counts()
        # 新批次：整体重建 BOM，把 L9 纳入叶件集合。
        resp = alpha.post(
            "/api/batch",
            json={
                "reset": True,
                "upserts": [
                    {"parent": "FG", "child": "SA", "qty": 2},
                    {"parent": "SA", "child": "L1", "qty": 3},
                    {"parent": "FG", "child": "L2", "qty": 4},
                    {"parent": "FG", "child": "L9", "qty": 1},
                ],
            },
        )
        assert resp.status_code == 200
        rules2 = _rules + "L9,箱,件,7,1\n"
        second = alpha.post(
            "/api/conversion/check", files=_files(detail, rules2)
        ).json()
        assert second["bom_version_seq"] > first["bom_version_seq"]
        rows = {r["line_id"]: r for r in second["rows"]}
        assert rows["D5"]["status"] == "valid"
        assert rows["D5"]["standard_qty"] == 7
        # 其余行结论保持不变。
        assert rows["D1"]["status"] == "valid"
        assert rows["D4"]["status"] == "rejected"
