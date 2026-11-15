"""领料明细到标准「件」的换算核查（纯领域逻辑，不依赖数据库与 Web）。

设计要点
--------

* 所有倍率都用 :class:`fractions.Fraction` 精确表示，全程不出现浮点；
  明细数量同样按十进制有理数解析（含 ``12.5`` 这类小数）。
* 换算规则 ``numerator/denominator`` 表示 ``1 from_unit = n/d to_unit``，
  反向自动推导为倒数，因此规则可双向、跨级串联。
* 对每个料号建立无向（双向边）倍率图，从明细单位出发枚举到「件」的所有
  路径，累计路径倍率。同一料号、同一明细单位若存在**不同**倍率，即判定
  规则矛盾：报告矛盾规则并隔离该料号的**全部**明细行。
* 状态机只有三种归宿：``valid``（有效行，可下载）、``quarantined``
  （料号规则矛盾，整料号隔离）、``rejected``（非当前 BOM 叶件 / 无换算
  路径 / 结果不是整数 / 明细行自身非法），每一行都带原因。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

PIECE_UNIT = "件"
MAX_DETAIL_ROWS = 5_000
MAX_RULE_ROWS = 2_000
# 单个 (单位, 倍率) 状态与单源路径枚举的硬上限，防止矛盾规则图指数爆炸。
MAX_GRAPH_STATES = 20_000
MAX_PATHS = 200


class ConversionError(Exception):
    """上传文件本身无法解析时抛出，由 Web 层映射为 422。"""


@dataclass(frozen=True)
class ConversionRule:
    leaf_part: str
    from_unit: str
    to_unit: str
    factor: Fraction  # 1 from_unit == factor * to_unit

    def as_dict(self) -> dict[str, Any]:
        return {
            "leaf_part": self.leaf_part,
            "from_unit": self.from_unit,
            "to_unit": self.to_unit,
            "numerator": self.factor.numerator,
            "denominator": self.factor.denominator,
        }


@dataclass(frozen=True)
class DetailLine:
    line_id: str
    leaf_part: str
    unit: str
    qty: Fraction
    source_row: int  # 1-based，含表头计数，便于用户回到原 CSV 定位


@dataclass
class CheckedLine:
    line_id: str
    leaf_part: str
    unit: str
    qty_text: str
    source_row: int
    status: str  # valid | quarantined | rejected
    standard_qty: int | None = None
    bom_version_seq: int | None = None
    reason: str = ""
    factor: str = ""  # 单位 -> 件 的精确倍率，如 1/12
    path: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PartConflict:
    leaf_part: str
    unit: str
    rates: list[str]  # 去重后的不同倍率（字符串形式）
    rules: list[dict[str, Any]]  # 参与矛盾路径的规则（去重）
    paths: list[list[str]]  # 倍率不同的两条代表路径


# --------------------------------------------------------------------------
# CSV 解析
# --------------------------------------------------------------------------


def _read_table(raw: bytes | str, label: str) -> tuple[list[str], list[dict[str, str]], int]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConversionError(f"{label} 必须是 UTF-8 编码的 CSV 文件") from exc
    else:
        text = raw
    if not text.strip():
        raise ConversionError(f"{label} 为空")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header = [cell.strip() for cell in rows[0]]
    body = rows[1:]
    dict_rows: list[dict[str, str]] = []
    for offset, cells in enumerate(body, start=2):  # 表头是第 1 行
        if not any(cell.strip() for cell in cells):
            continue  # 跳过空行
        if len(cells) < len(header):
            cells = cells + [""] * (len(header) - len(cells))
        dict_rows.append({header[i]: cells[i].strip() for i in range(len(header))})
    return header, dict_rows, len(dict_rows)


def _require_columns(header: Sequence[str], required: Sequence[str], label: str) -> None:
    missing = [name for name in required if name not in header]
    if missing:
        raise ConversionError(f"{label} 缺少必需列：{', '.join(missing)}（实际列：{', '.join(header)}）")


def parse_decimal(text: str, field_name: str, row: int) -> Fraction:
    text = (text or "").strip()
    if not text:
        raise ConversionError(f"第 {row} 行 {field_name} 为空")
    sign = 1
    body = text
    if body[0] in "+-":
        if body[0] == "-":
            sign = -1
        body = body[1:]
    if not body or any(not (ch.isdigit() or ch == ".") for ch in body) or body.count(".") > 1:
        raise ConversionError(f"第 {row} 行 {field_name} 不是合法数字：{text!r}")
    whole, dot, frac = body.partition(".")
    digits = whole + frac
    if not digits:
        raise ConversionError(f"第 {row} 行 {field_name} 不是合法数字：{text!r}")
    denominator = 10 ** len(frac) if dot else 1
    if sign < 0 or int(digits) == 0:
        raise ConversionError(f"第 {row} 行 {field_name} 必须是正数：{text!r}")
    # 直接由整数分子/分母构造，绝不经过 float，保证精确。
    return Fraction(int(digits), denominator)


def parse_positive_int(text: str, field_name: str, row: int) -> int:
    text = (text or "").strip()
    if not text.isdigit() or int(text) < 1:
        raise ConversionError(f"第 {row} 行 {field_name} 必须是正整数：{text!r}")
    return int(text)


def _require_cell(row: Mapping[str, str], name: str, row_no: int, label: str) -> str:
    value = (row.get(name) or "").strip()
    if not value:
        raise ConversionError(f"{label}第 {row_no} 行 {name} 为空")
    return value


def parse_detail_csv(raw: bytes | str) -> list[DetailLine]:
    """解析领料 CSV（line_id, leaf_part, unit, qty）。"""
    header, rows, count = _read_table(raw, "领料明细 CSV")
    _require_columns(header, ("line_id", "leaf_part", "unit", "qty"), "领料明细 CSV")
    if count > MAX_DETAIL_ROWS:
        raise ConversionError(f"领料明细最多 {MAX_DETAIL_ROWS} 行，实际 {count} 行")
    lines: list[DetailLine] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        line_id = _require_cell(row, "line_id", index, "领料明细")
        leaf_part = _require_cell(row, "leaf_part", index, "领料明细")
        unit = _require_cell(row, "unit", index, "领料明细")
        qty = parse_decimal(row.get("qty", ""), "qty", index)
        if line_id in seen_ids:
            raise ConversionError(f"领料明细第 {index} 行 line_id 重复：{line_id}")
        seen_ids.add(line_id)
        lines.append(DetailLine(line_id, leaf_part, unit, qty, index))
    if not lines:
        raise ConversionError("领料明细 CSV 没有数据行")
    return lines


def parse_rules_csv(raw: bytes | str) -> list[ConversionRule]:
    """解析料号专属换算 CSV。"""
    header, rows, count = _read_table(raw, "换算规则 CSV")
    _require_columns(
        header, ("leaf_part", "from_unit", "to_unit", "numerator", "denominator"), "换算规则 CSV"
    )
    if count > MAX_RULE_ROWS:
        raise ConversionError(f"换算规则最多 {MAX_RULE_ROWS} 行，实际 {count} 行")
    rules: list[ConversionRule] = []
    for index, row in enumerate(rows, start=2):
        leaf_part = _require_cell(row, "leaf_part", index, "换算规则")
        from_unit = _require_cell(row, "from_unit", index, "换算规则")
        to_unit = _require_cell(row, "to_unit", index, "换算规则")
        numerator = parse_positive_int(row.get("numerator", ""), "numerator", index)
        denominator = parse_positive_int(row.get("denominator", ""), "denominator", index)
        if from_unit == to_unit:
            if numerator != denominator:
                raise ConversionError(
                    f"换算规则第 {index} 行 from_unit 与 to_unit 相同（{from_unit}），"
                    "倍率必须为 1，否则单位自相矛盾"
                )
            continue  # 1:1 自环规则没有信息量
        rules.append(
            ConversionRule(leaf_part, from_unit, to_unit, Fraction(numerator, denominator))
        )
    return rules


# --------------------------------------------------------------------------
# 倍率图
# --------------------------------------------------------------------------


def _build_graph(rules: Iterable[ConversionRule]) -> dict[str, list[tuple[str, Fraction, ConversionRule]]]:
    """无向倍率图：每条规则同时登记正向边与反向（倒数）边。"""
    graph: dict[str, list[tuple[str, Fraction, ConversionRule]]] = {}
    for rule in rules:
        graph.setdefault(rule.from_unit, []).append((rule.to_unit, rule.factor, rule))
        graph.setdefault(rule.to_unit, []).append((rule.from_unit, 1 / rule.factor, rule))
    for edges in graph.values():
        edges.sort(key=lambda edge: edge[0])
    return graph


def _format_rate(rate: Fraction) -> str:
    return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"


def _format_path(steps: Sequence[tuple[str, str]], rate: Fraction) -> str:
    chain = " → ".join(f"{a}→{b}" for a, b in steps)
    return f"{chain}（倍率 {_format_rate(rate)}）"


def _rate_paths(
    graph: Mapping[str, list[tuple[str, Fraction, ConversionRule]]],
    source: str,
    target: str,
    part: str = "",
) -> list[tuple[Fraction, list[tuple[str, str, ConversionRule]]]]:
    """枚举 source 到 target 的全部简单路径及其连乘倍率（带硬上限）。

    返回 ``(倍率, 路径步骤)`` 列表，步骤为 ``(from_unit, to_unit, rule)``。
    """
    results: list[tuple[Fraction, list[tuple[str, str, ConversionRule]]]] = []
    if source == target:
        return [(Fraction(1), [])]
    if source not in graph:
        return results
    # 迭代 DFS，帧：(当前单位, 累计倍率, 已访问单位, 路径步骤, 邻接下标)
    stack: list[list[Any]] = [[source, Fraction(1), {source}, [], 0]]
    states = 1
    while stack:
        frame = stack[-1]
        node, acc, visited, steps, index = frame
        neighbours = graph.get(node, [])
        if index >= len(neighbours) or len(results) >= MAX_PATHS:
            stack.pop()
            continue
        frame[4] = index + 1
        nxt, edge_factor, rule = neighbours[index]
        if nxt in visited:
            continue
        states += 1
        if states > MAX_GRAPH_STATES:
            raise ConversionError(
                f"换算图路径数量超过上限 {MAX_GRAPH_STATES}，请检查料号 {part or '?'} "
                f"单位 {source} 的换算规则是否存在大量环路"
            )
        new_rate = acc * edge_factor
        if nxt == target:
            results.append((new_rate, steps + [(node, nxt, rule)]))
            if len(results) >= MAX_PATHS:
                break
            continue
        stack.append([nxt, new_rate, visited | {nxt}, steps + [(node, nxt, rule)], 0])
    return results


def resolve_part(
    rules: Sequence[ConversionRule], source_unit: str
) -> tuple[Fraction | None, list[tuple[str, str]], list[PartConflict], list[dict[str, str]]]:
    """解析单个明细单位到「件」的倍率。

    返回 ``(倍率, 展示路径, 该料号的矛盾列表, JSON 路径)``；倍率为 ``None``
    表示无换算路径或规则矛盾。矛盾按明细单位归集（同一料号可能有多个单位
    各自矛盾）。
    """
    graph = _build_graph(rules)
    part = rules[0].leaf_part if rules else ""
    paths = _rate_paths(graph, source_unit, PIECE_UNIT, part)
    if not paths:
        return None, [], [], []
    distinct_rates = {rate for rate, _ in paths}
    example_steps = [(a, b) for a, b, _ in paths[0][1]]
    json_path = [
        {"from_unit": a, "to_unit": b, "rule": f"{r.leaf_part}:{r.from_unit}→{r.to_unit}"}
        for a, b, r in paths[0][1]
    ]
    if len(distinct_rates) == 1:
        return paths[0][0], example_steps, [], json_path

    # 矛盾：挑两条倍率不同的代表路径，并汇总涉及的规则。
    chosen: dict[Fraction, tuple[Fraction, list[tuple[str, str, ConversionRule]]]] = {}
    for rate, steps in paths:
        chosen.setdefault(rate, (rate, steps))
        if len(chosen) == len(distinct_rates):
            break
    representative = list(chosen.values())[:2]
    involved: dict[tuple[str, str, str, int, int], dict[str, Any]] = {}
    for _, steps in representative:
        for _, _, rule in steps:
            key = (rule.leaf_part, rule.from_unit, rule.to_unit, rule.factor.numerator, rule.factor.denominator)
            involved.setdefault(key, rule.as_dict())
    leaf = rules[0].leaf_part if rules else source_unit
    conflict = PartConflict(
        leaf_part=leaf,
        unit=source_unit,
        rates=sorted((_format_rate(rate) for rate in distinct_rates), key=lambda s: (len(s), s)),
        rules=list(involved.values()),
        paths=[
            [source_unit] + [b for _, b, _ in steps]
            for _, steps in representative
        ],
    )
    return None, example_steps, [conflict], json_path


# --------------------------------------------------------------------------
# 整体核查
# --------------------------------------------------------------------------


def check_conversion(
    lines: Sequence[DetailLine],
    rules: Sequence[ConversionRule],
    leaf_parts: frozenset[str],
    version_seq: int,
) -> dict[str, Any]:
    """对整批领料明细执行换算核查。

    ``leaf_parts`` / ``version_seq`` 来自**同一次只读事务**解析出的已发布
    BOM 快照，因此整份报告只依据一个已提交版本，与并发批改互不串版本。
    """
    rules_by_part: dict[str, list[ConversionRule]] = {}
    for rule in rules:
        rules_by_part.setdefault(rule.leaf_part, []).append(rule)

    # 第一遍：按料号解析每个明细单位，确定矛盾料号与可用倍率。
    conflicts_by_part: dict[str, list[PartConflict]] = {}
    resolution: dict[tuple[str, str], tuple[Fraction | None, list[tuple[str, str]], list[dict[str, str]]]] = {}
    units_by_part: dict[str, set[str]] = {}
    for line in lines:
        units_by_part.setdefault(line.leaf_part, set()).add(line.unit)
    for part, units in units_by_part.items():
        part_rules = rules_by_part.get(part, [])
        for unit in sorted(units):
            rate, steps, conflicts, json_path = resolve_part(part_rules, unit)
            resolution[(part, unit)] = (rate, steps, json_path)
            for conflict in conflicts:
                conflict.leaf_part = part
                conflicts_by_part.setdefault(part, []).append(conflict)

    checked: list[CheckedLine] = []
    counts = {"valid": 0, "quarantined": 0, "rejected": 0}
    for line in lines:
        item = CheckedLine(
            line_id=line.line_id,
            leaf_part=line.leaf_part,
            unit=line.unit,
            qty_text=_fraction_text(line.qty),
            source_row=line.source_row,
            status="rejected",
        )
        if line.leaf_part not in leaf_parts:
            item.reason = f"料号 {line.leaf_part} 不是当前已发布 BOM（v{version_seq}）的叶件，不予处理"
        elif line.leaf_part in conflicts_by_part:
            item.status = "quarantined"
            item.bom_version_seq = version_seq
            item.reason = (
                f"料号 {line.leaf_part} 的换算规则矛盾：单位 {line.unit} 到「{PIECE_UNIT}」"
                "存在不同倍率，整料号明细已隔离"
            )
        else:
            rate, steps, json_path = resolution[(line.leaf_part, line.unit)]
            if rate is None:
                item.reason = (
                    f"找不到 {line.leaf_part} 从单位 {line.unit} 到「{PIECE_UNIT}」的换算路径"
                )
            else:
                standard = line.qty * rate
                item.factor = _format_rate(rate)
                item.path = json_path
                item.bom_version_seq = version_seq
                if standard.denominator != 1:
                    item.reason = (
                        f"换算结果不是整数：{_fraction_text(line.qty)} {line.unit} × "
                        f"{_format_rate(rate)} = {_fraction_text(standard)} {PIECE_UNIT}"
                        + ("（" + _format_path(steps, rate) + "）" if steps else "")
                    )
                else:
                    item.status = "valid"
                    item.standard_qty = standard.numerator
                    item.reason = ""
        counts[item.status] += 1
        checked.append(item)

    conflicts = [conflict for part_conflicts in conflicts_by_part.values() for conflict in part_conflicts]
    return {
        "bom_version_seq": version_seq,
        "piece_unit": PIECE_UNIT,
        "counts": counts,
        "total_lines": len(checked),
        "conflicts": [_conflict_dict(conflict) for conflict in conflicts],
        "quarantined_parts": sorted(conflicts_by_part),
        "rows": [_line_dict(item) for item in checked],
    }


def valid_rows_csv(report: Mapping[str, Any]) -> str:
    """把有效行渲染为可下载 CSV（带版本列）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["line_id", "leaf_part", "unit", "qty", "standard_qty", "factor", "bom_version_seq"]
    )
    for row in report["rows"]:
        if row["status"] != "valid":
            continue
        writer.writerow(
            [
                row["line_id"],
                row["leaf_part"],
                row["unit"],
                row["qty_text"],
                row["standard_qty"],
                row["factor"],
                row["bom_version_seq"],
            ]
        )
    return buffer.getvalue()


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    if _terminating_decimal(value):
        # 用整数除法精确还原有限小数，避免大数字走 float 丢精度。
        whole, rem = divmod(value.numerator, value.denominator)
        if rem == 0:
            return str(whole)
        digits = []
        for _ in range(_decimal_places(value.denominator)):
            rem *= 10
            digit, rem = divmod(rem, value.denominator)
            digits.append(str(digit))
        return f"{whole}.{''.join(digits).rstrip('0')}"
    return f"{value.numerator}/{value.denominator}"


def _terminating_decimal(value: Fraction) -> bool:
    denominator = value.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    return denominator == 1


def _decimal_places(denominator: int) -> int:
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    return max(twos, fives)


def _line_dict(item: CheckedLine) -> dict[str, Any]:
    return {
        "line_id": item.line_id,
        "leaf_part": item.leaf_part,
        "unit": item.unit,
        "qty_text": item.qty_text,
        "source_row": item.source_row,
        "status": item.status,
        "standard_qty": item.standard_qty,
        "bom_version_seq": item.bom_version_seq,
        "reason": item.reason,
        "factor": item.factor,
        "path": item.path,
    }


def _conflict_dict(conflict: PartConflict) -> dict[str, Any]:
    return {
        "leaf_part": conflict.leaf_part,
        "unit": conflict.unit,
        "rates": conflict.rates,
        "rules": conflict.rules,
        "paths": [" → ".join(units) for units in conflict.paths],
    }
