"""换算核查领域逻辑测试：有理数精确计算、双向跨级推导、矛盾隔离。"""

from __future__ import annotations

import pytest

from app.conversion import (
    MAX_DETAIL_ROWS,
    ConversionError,
    ConversionRule,
    check_conversion,
    parse_decimal,
    parse_detail_csv,
    parse_rules_csv,
    valid_rows_csv,
)
from fractions import Fraction

LEAVES = frozenset({"L1", "L2", "L3", "L4"})


def _rules(*tuples):
    return [
        ConversionRule(part, src, dst, Fraction(num, den))
        for part, src, dst, num, den in tuples
    ]


def _report(detail_text, rules_text, leaves=LEAVES, version=4):
    return check_conversion(
        parse_detail_csv(detail_text), parse_rules_csv(rules_text), leaves, version
    )


DETAIL_HEADER = "line_id,leaf_part,unit,qty\n"
RULE_HEADER = "leaf_part,from_unit,to_unit,numerator,denominator\n"


# -- CSV 解析 -------------------------------------------------------------


def test_parse_decimal_is_exact_rational_never_float():
    assert parse_decimal("0.1", "qty", 2) == Fraction(1, 10)
    assert parse_decimal("12.50", "qty", 2) == Fraction(25, 2)
    assert parse_decimal("3", "qty", 2) == Fraction(3)
    assert parse_decimal(" 7.25 ", "qty", 2) == Fraction(29, 4)


@pytest.mark.parametrize("bad", ["", "0", "-1", "abc", "1.2.3", "1/3", "-0.5"])
def test_parse_decimal_rejects_bad_numbers(bad):
    with pytest.raises(ConversionError):
        parse_decimal(bad, "qty", 3)


def test_detail_csv_requires_columns_and_positive_qty():
    with pytest.raises(ConversionError, match="缺少必需列"):
        parse_detail_csv("line_id,leaf_part,unit\nx,L1,箱\n")
    with pytest.raises(ConversionError, match="qty"):
        parse_detail_csv(DETAIL_HEADER + "x,L1,箱,0\n")
    with pytest.raises(ConversionError, match="line_id 重复"):
        parse_detail_csv(DETAIL_HEADER + "x,L1,箱,1\nx,L2,包,2\n")
    with pytest.raises(ConversionError, match="为空"):
        parse_detail_csv(DETAIL_HEADER + "x,,箱,1\n")


def test_rules_csv_rejects_non_positive_rational_and_contradictory_self_loop():
    with pytest.raises(ConversionError, match="正整数"):
        parse_rules_csv(RULE_HEADER + "L1,箱,件,0,1\n")
    with pytest.raises(ConversionError, match="denominator"):
        parse_rules_csv(RULE_HEADER + "L1,箱,件,1,0\n")
    with pytest.raises(ConversionError, match="自相矛盾"):
        parse_rules_csv(RULE_HEADER + "L1,箱,箱,2,1\n")
    # 1:1 自环规则被静默忽略，不报错。
    assert parse_rules_csv(RULE_HEADER + "L1,箱,箱,1,1\n") == []


def test_empty_and_oversized_uploads_are_rejected():
    with pytest.raises(ConversionError):
        parse_detail_csv("")
    big = DETAIL_HEADER + "".join(f"D{i},L1,箱,1\n" for i in range(MAX_DETAIL_ROWS + 1))
    with pytest.raises(ConversionError, match="最多"):
        parse_detail_csv(big)


# -- 换算路径 -------------------------------------------------------------


def test_multi_level_and_reverse_derivation_are_exact():
    detail = DETAIL_HEADER + "D1,L1,箱,2\nD2,L1,件,5\nD3,L1,包,1\n"
    # 箱→包=10、包→件=12：1 箱 = 120 件；件→包反向 = 1/12。
    rules = RULE_HEADER + "L1,箱,包,10,1\nL1,包,件,12,1\n"
    report = _report(detail, rules)
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["status"] == "valid"
    assert rows["D1"]["standard_qty"] == 240
    assert rows["D1"]["factor"] == "120"
    assert [step["to_unit"] for step in rows["D1"]["path"]] == ["包", "件"]
    assert rows["D2"]["standard_qty"] == 5 and rows["D2"]["factor"] == "1"
    assert rows["D3"]["standard_qty"] == 12
    assert report["counts"] == {"valid": 3, "quarantined": 0, "rejected": 0}


def test_reverse_rule_used_when_only_backward_edge_exists():
    # 只登记 件→包=12，明细单位是包：需要走反向边 1/12…再配合 包 视角推导。
    detail = DETAIL_HEADER + "D1,L1,件,24\n"
    rules = RULE_HEADER + "L1,件,包,12,1\n"
    report = _report(detail, rules)
    row = report["rows"][0]
    assert row["status"] == "valid"
    assert row["standard_qty"] == 24  # 件本身即目标单位


def test_non_integer_result_is_rejected_with_reason_but_other_lines_continue():
    detail = DETAIL_HEADER + "D1,L2,盒,1\nD2,L2,盒,3\nD3,L1,箱,1\n"
    rules = (
        RULE_HEADER
        + "L2,盒,件,1,3\n"   # 1 盒 = 1/3 件
        + "L1,箱,件,10,1\n"
    )
    report = _report(detail, rules)
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["status"] == "rejected"
    assert "不是整数" in rows["D1"]["reason"]
    assert "1/3" in rows["D1"]["reason"]
    # 3 盒 × 1/3 = 1 件，整数，有效。
    assert rows["D2"]["status"] == "valid" and rows["D2"]["standard_qty"] == 1
    assert rows["D3"]["status"] == "valid" and rows["D3"]["standard_qty"] == 10
    assert report["counts"]["rejected"] == 1


def test_missing_conversion_path_is_rejected():
    detail = DETAIL_HEADER + "D1,L1,托盘,2\n"
    rules = RULE_HEADER + "L1,箱,件,10,1\n"
    report = _report(detail, rules)
    row = report["rows"][0]
    assert row["status"] == "rejected"
    assert "找不到" in row["reason"] and "托盘" in row["reason"]


def test_non_leaf_parts_are_not_processed():
    detail = DETAIL_HEADER + "D1,SA-9,箱,1\nD2,L1,箱,1\n"
    rules = RULE_HEADER + "SA-9,箱,件,10,1\nL1,箱,件,10,1\n"
    report = _report(detail, rules, leaves=frozenset({"L1"}), version=9)
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["status"] == "rejected"
    assert "不是当前已发布 BOM" in rows["D1"]["reason"]
    assert "v9" in rows["D1"]["reason"]
    assert rows["D2"]["status"] == "valid"
    assert rows["D2"]["bom_version_seq"] == 9
    assert rows["D1"]["bom_version_seq"] is None


# -- 规则矛盾 -------------------------------------------------------------


def test_conflicting_rates_quarantine_all_lines_of_the_part():
    detail = DETAIL_HEADER + "D1,L3,箱,1\nD2,L3,箱,2\nD3,L1,箱,1\n"
    rules = (
        RULE_HEADER
        + "L3,箱,件,1,1\n"          # 直达：1 箱 = 1 件
        + "L3,箱,包,4,1\n"
        + "L3,包,件,3,1\n"         # 跨级：1 箱 = 12 件
        + "L1,箱,件,10,1\n"
    )
    report = _report(detail, rules)
    rows = {r["line_id"]: r for r in report["rows"]}
    assert rows["D1"]["status"] == "quarantined"
    assert rows["D2"]["status"] == "quarantined"
    assert "矛盾" in rows["D1"]["reason"]
    # 其他料号不受影响，仍可产出标准件数。
    assert rows["D3"]["status"] == "valid" and rows["D3"]["standard_qty"] == 10
    assert report["quarantined_parts"] == ["L3"]
    conflict = report["conflicts"][0]
    assert conflict["leaf_part"] == "L3"
    assert set(conflict["rates"]) == {"1", "12"}
    assert len(conflict["rules"]) == 3
    assert any("包" in path for path in conflict["paths"])


def test_consistent_alternate_paths_are_not_conflicts():
    # 两条路径倍率一致：直达 12，与 箱→包(4)→件(3)=12 相同。
    detail = DETAIL_HEADER + "D1,L3,箱,1\n"
    rules = (
        RULE_HEADER
        + "L3,箱,件,12,1\n"
        + "L3,箱,包,4,1\n"
        + "L3,包,件,3,1\n"
    )
    report = _report(detail, rules)
    assert report["conflicts"] == []
    assert report["rows"][0]["status"] == "valid"
    assert report["rows"][0]["standard_qty"] == 12


def test_conflict_on_second_unit_still_quarantines_whole_part():
    # 箱单位只有一条一致路径（箱→包→件=120）；包单位另有矛盾倍率 6 → 整料号隔离。
    detail = DETAIL_HEADER + "D1,L3,箱,1\nD2,L3,包,1\n"
    rules = (
        RULE_HEADER
        + "L3,箱,包,10,1\n"
        + "L3,包,件,12,1\n"        # 箱→包→件 = 120；包→件 = 12
        + "L3,包,打,1,2\n"
        + "L3,打,件,12,1\n"        # 包→打(1/2)→件(12) = 6，与 12 矛盾
    )
    report = _report(detail, rules)
    assert report["quarantined_parts"] == ["L3"]
    assert all(r["status"] == "quarantined" for r in report["rows"])
    # 矛盾源是包单位（12 vs 6）；箱单位经包到件同样派生两条倍率，一并报出。
    units = {c["unit"] for c in report["conflicts"]}
    assert units == {"包", "箱"}
    assert {"6", "12"} <= {rate for conflict in report["conflicts"] for rate in conflict["rates"]}


# -- 输出 -----------------------------------------------------------------


def test_valid_rows_csv_contains_only_valid_lines_with_version():
    detail = (
        DETAIL_HEADER
        + "D1,L1,箱,2\n"
        + "D2,L2,盒,1\n"   # 非整数，拒收
        + "D3,L1,件,3\n"
    )
    rules = RULE_HEADER + "L1,箱,包,10,1\nL1,包,件,12,1\nL2,盒,件,1,3\n"
    report = _report(detail, rules, version=11)
    csv_text = valid_rows_csv(report)
    lines = [line for line in csv_text.strip().split("\n")]
    assert lines[0] == "line_id,leaf_part,unit,qty,standard_qty,factor,bom_version_seq"
    assert "D1,L1,箱,2,240,120,11" in lines
    assert "D3,L1,件,3,3,1,11" in lines
    assert not any(line.startswith("D2") for line in lines)


def test_every_row_carries_the_same_committed_bom_version():
    detail = DETAIL_HEADER + "D1,L1,箱,1\nD2,NOPE,箱,1\n"
    rules = RULE_HEADER + "L1,箱,件,10,1\n"
    report = _report(detail, rules, version=21)
    assert report["bom_version_seq"] == 21
    versioned = [r for r in report["rows"] if r["bom_version_seq"] is not None]
    assert {r["bom_version_seq"] for r in versioned} == {21}


def test_fractional_qty_multiplied_exactly():
    # 0.1 箱 × 120 件/箱 = 12 件（若用 float 会出现 12.000000000000002 类误差）。
    detail = DETAIL_HEADER + "D1,L1,箱,0.1\n"
    rules = RULE_HEADER + "L1,箱,包,10,1\nL1,包,件,12,1\n"
    report = _report(detail, rules)
    row = report["rows"][0]
    assert row["status"] == "valid"
    assert row["standard_qty"] == 12


def test_reverse_derivation_uses_reciprocal_of_registered_rule():
    # 只登记 件→箱=10，箱的行必须走反向边（倍率 1/10），20 箱 = 2 件。
    detail = DETAIL_HEADER + "D1,L1,箱,20\n"
    rules = RULE_HEADER + "L1,件,箱,10,1\n"
    report = _report(detail, rules)
    row = report["rows"][0]
    assert row["status"] == "valid"
    assert row["standard_qty"] == 2
    assert row["factor"] == "1/10"
    assert row["path"][0] == {"from_unit": "箱", "to_unit": "件", "rule": "L1:件→箱"}


def test_duplicate_identical_rules_are_not_conflicts():
    detail = DETAIL_HEADER + "D1,L1,箱,1\n"
    rules = RULE_HEADER + "L1,箱,件,10,1\nL1,箱,件,10,1\n"
    report = _report(detail, rules)
    assert report["conflicts"] == []
    assert report["rows"][0]["status"] == "valid"
    assert report["rows"][0]["standard_qty"] == 10


def test_empty_bom_rejects_every_part_as_non_leaf():
    detail = DETAIL_HEADER + "D1,L1,箱,1\n"
    rules = RULE_HEADER + "L1,箱,件,10,1\n"
    report = _report(detail, rules, leaves=frozenset(), version=1)
    row = report["rows"][0]
    assert row["status"] == "rejected"
    assert "不是当前已发布 BOM" in row["reason"]
    assert row["bom_version_seq"] is None
