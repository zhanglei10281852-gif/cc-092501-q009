"""模型版本结果比较的纯计算逻辑。

覆盖两类科学计算结果：

- ``inversion``：补给端元比例、质量平衡与拟合残差（rmse），比较前先统一端元；
- ``transport``：污染浓度曲线、峰值浓度与到达时间，比较前先统一时间轴。

每个被比较的数值量都会给出绝对差、带符号相对差和（可能为空的）区间重叠。
当两侧数据或参数无法支持某项比较时，对应字段进入 ``incomparable`` 列表并
注明原因，而不是抛出异常中断整份报告。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any

# 浮点比较与相对差的最小分母，避免除零放大。
_EPS = 1e-12

# 迁移结果中决定物理情景的参数；任一不同则曲线差异不能归因于模型版本。
_TRANSPORT_PHYSICAL_PARAMS = (
    "source_concentration",
    "distance_m",
    "velocity_m_day",
    "dispersion_m2_day",
    "decay_per_day",
)
# 仅影响采样网格、不改变物理情景的参数。
_TRANSPORT_GRID_PARAMS = ("duration_days", "step_days")


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _relative_interval(value: float, band: float | None) -> list[float] | None:
    """以相对半宽 ``band``（如 0.05 表示 ±5%）构造闭区间。"""
    if band is None or not _finite(value) or not _finite(band):
        return None
    low = value * (1.0 - band)
    high = value * (1.0 + band)
    return [min(low, high), max(low, high)]


def interval_overlap(
    base_interval: list[float], candidate_interval: list[float]
) -> dict[str, Any]:
    """计算两个闭区间的重叠情况。

    返回是否重叠、重叠长度、Jaccard 系数（交集/并集）以及较窄区间被覆盖的
    比例（overlap ratio），后者在两个区间宽度差异较大时更直观。
    """
    b_lo, b_hi = sorted(base_interval[:2])
    c_lo, c_hi = sorted(candidate_interval[:2])
    inter_lo = max(b_lo, c_lo)
    inter_hi = min(b_hi, c_hi)
    overlap = max(0.0, inter_hi - inter_lo)
    union_lo = min(b_lo, c_lo)
    union_hi = max(b_hi, c_hi)
    union = max(0.0, union_hi - union_lo)
    base_width = max(0.0, b_hi - b_lo)
    candidate_width = max(0.0, c_hi - c_lo)
    narrow = min(base_width, candidate_width)
    if union <= _EPS:
        jaccard = 1.0 if abs(b_lo - c_lo) <= _EPS else 0.0
    else:
        jaccard = overlap / union
    overlap_ratio = overlap / narrow if narrow > _EPS else (1.0 if overlap >= -_EPS else 0.0)
    return {
        "baseline_interval": [b_lo, b_hi],
        "candidate_interval": [c_lo, c_hi],
        "overlaps": inter_hi >= inter_lo - _EPS,
        "overlap_length": overlap,
        "jaccard": jaccard,
        "overlap_ratio": overlap_ratio,
    }


def _threshold_change(
    baseline: float,
    candidate: float,
    limit: float,
    direction: str,
) -> dict[str, Any]:
    """关键阈值越界状态变化：``improved`` / ``worsened`` / ``stays_*``。"""
    if direction == "max":
        base_violated = baseline > limit + _EPS
        cand_violated = candidate > limit + _EPS
    else:  # direction == "min"
        base_violated = baseline < limit - _EPS
        cand_violated = candidate < limit - _EPS
    if base_violated == cand_violated:
        change = "stays_violated" if cand_violated else "stays_within"
    else:
        change = "worsened" if cand_violated else "improved"
    return {
        "limit": limit,
        "direction": direction,
        "baseline_violated": base_violated,
        "candidate_violated": cand_violated,
        "change": change,
    }


def compare_scalar(
    metric: str,
    baseline: Any,
    candidate: Any,
    *,
    baseline_interval: list[float] | None = None,
    candidate_interval: list[float] | None = None,
    threshold: float | None = None,
    threshold_direction: str = "max",
    incomparable_reason: str | None = None,
) -> dict[str, Any]:
    """比较两个标量，产出稳定结构的差异行。"""
    row: dict[str, Any] = {
        "metric": metric,
        "baseline": baseline,
        "candidate": candidate,
        "comparable": True,
        "absolute_difference": None,
        "signed_difference": None,
        "relative_difference": None,
        "interval_overlap": None,
        "threshold_crossing": None,
        "reason": None,
    }
    if incomparable_reason is not None:
        row["comparable"] = False
        row["reason"] = incomparable_reason
        return row
    if not (_finite(baseline) and _finite(candidate)):
        row["comparable"] = False
        row["reason"] = "non_numeric_value"
        return row

    signed = float(candidate) - float(baseline)
    row["signed_difference"] = signed
    row["absolute_difference"] = abs(signed)
    if abs(baseline) > _EPS:
        row["relative_difference"] = signed / abs(baseline)
    else:
        # 基线为 0 时相对差没有定义，明确标注而不是返回 inf。
        row["reason"] = "relative_difference_undefined_zero_baseline"

    if baseline_interval is not None and candidate_interval is not None:
        row["interval_overlap"] = interval_overlap(baseline_interval, candidate_interval)

    if threshold is not None and _finite(threshold):
        row["threshold_crossing"] = _threshold_change(
            float(baseline), float(candidate), float(threshold), threshold_direction
        )
    return row


def _fraction_interval(
    fraction: float, endmember: dict[str, Any] | None, band: float | None
) -> list[float] | None:
    """端元比例的不确定区间。

    显式给出 ``uncertainty_band`` 时使用统一相对半宽；否则回退到端元自身的
    ``uncertainty`` 标称不确定度，按比例构造包络并裁剪到 [0, 1]。
    """
    if band is not None:
        interval = _relative_interval(fraction, band)
        if interval is None:
            return None
        return [max(0.0, interval[0]), min(1.0, interval[1])]
    if endmember is not None and _finite(endmember.get("uncertainty")):
        delta = fraction * float(endmember["uncertainty"])
        return [max(0.0, fraction - delta), min(1.0, fraction + delta)]
    return None


def _align_fractions(
    base_result: dict[str, Any],
    cand_result: dict[str, Any],
    base_endmembers: dict[int, dict[str, Any]],
    cand_endmembers: dict[int, dict[str, Any]],
    band: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """统一端元后比较补给比例，返回 (比例行, 不可比项)。

    优先按端元 id 对齐；id 集合不一致时回退到同名端元对齐。只在一侧出现的
    端元无法比较，进入不可比列表。返回的比例行按 baseline 端元 id 升序，保证
    明细稳定排序。
    """
    base_fractions = {int(f["endmember_id"]): f for f in base_result.get("fractions", [])}
    cand_fractions = {int(f["endmember_id"]): f for f in cand_result.get("fractions", [])}

    pairs: list[tuple[int, int]] = []
    matched_candidate: set[int] = set()
    for base_id in sorted(base_fractions):
        if base_id in cand_fractions:
            pairs.append((base_id, base_id))
            matched_candidate.add(base_id)
            continue
        base_name = base_fractions[base_id].get("name")
        for cand_id in sorted(cand_fractions):
            if cand_id in matched_candidate:
                continue
            if cand_fractions[cand_id].get("name") == base_name:
                pairs.append((base_id, cand_id))
                matched_candidate.add(cand_id)
                break

    rows: list[dict[str, Any]] = []
    for base_id, cand_id in pairs:
        base_fraction = float(base_fractions[base_id]["fraction"])
        cand_fraction = float(cand_fractions[cand_id]["fraction"])
        name = base_fractions[base_id].get("name") or cand_fractions[cand_id].get("name")
        row = compare_scalar(
            f"fraction.{base_id}",
            base_fraction,
            cand_fraction,
            baseline_interval=_fraction_interval(base_fraction, base_endmembers.get(base_id), band),
            candidate_interval=_fraction_interval(
                cand_fraction, cand_endmembers.get(cand_id), band
            ),
        )
        row["endmember"] = {
            "name": name,
            "baseline_endmember_id": base_id,
            "candidate_endmember_id": cand_id,
        }
        rows.append(row)

    incomparable: list[dict[str, Any]] = []
    paired_base = {base_id for base_id, _ in pairs}
    for base_id in sorted(base_fractions):
        if base_id not in paired_base:
            incomparable.append(
                {
                    "field": f"fraction.{base_id}",
                    "reason": "endmember_only_in_baseline",
                    "detail": {"endmember": base_fractions[base_id].get("name")},
                }
            )
    for cand_id in sorted(cand_fractions):
        if cand_id not in matched_candidate:
            incomparable.append(
                {
                    "field": f"fraction.{cand_id}",
                    "reason": "endmember_only_in_candidate",
                    "detail": {"endmember": cand_fractions[cand_id].get("name")},
                }
            )
    return rows, incomparable


def compare_inversion(
    base_input: dict[str, Any],
    base_result: dict[str, Any],
    cand_input: dict[str, Any],
    cand_result: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """比较同一支样本上两个反演模型版本的结果。"""
    thresholds = options.get("thresholds") or {}
    band = options.get("uncertainty_band")
    base_endmembers = {int(e["id"]): e for e in base_input.get("endmembers", [])}
    cand_endmembers = {int(e["id"]): e for e in cand_input.get("endmembers", [])}

    fraction_rows, incomparable = _align_fractions(
        base_result, cand_result, base_endmembers, cand_endmembers, band
    )

    # 同一端元 id 在两次运行之间定义（同位素/溶质）若发生变化，该端元比例虽能
    # 按 id 对齐，但比较的已不是同一物理量，标记该字段无法比较。
    signature_keys = ("isotope_d18o", "isotope_d2h", "solute_mg_l")
    for endmember_id in sorted(set(base_endmembers) & set(cand_endmembers)):
        base_em = base_endmembers[endmember_id]
        cand_em = cand_endmembers[endmember_id]
        changed = next((key for key in signature_keys if not _close(base_em.get(key), cand_em.get(key))), None)
        if changed is not None:
            incomparable.append(
                {
                    "field": f"fraction.{endmember_id}",
                    "reason": "endmember_definition_mismatch",
                    "detail": {
                        "endmember": base_em.get("name"),
                        "parameter": changed,
                        "baseline": base_em.get(changed),
                        "candidate": cand_em.get(changed),
                    },
                }
            )

    metrics: list[dict[str, Any]] = list(fraction_rows)
    metrics.append(
        compare_scalar(
            "mass_balance",
            base_result.get("mass_balance"),
            cand_result.get("mass_balance"),
            baseline_interval=_relative_interval(
                float(base_result["mass_balance"]), band
            )
            if _finite(base_result.get("mass_balance"))
            else None,
            candidate_interval=_relative_interval(
                float(cand_result["mass_balance"]), band
            )
            if _finite(cand_result.get("mass_balance"))
            else None,
        )
    )
    metrics.append(
        compare_scalar(
            "rmse",
            base_result.get("rmse"),
            cand_result.get("rmse"),
            baseline_interval=_relative_interval(float(base_result["rmse"]), band)
            if _finite(base_result.get("rmse"))
            else None,
            candidate_interval=_relative_interval(float(cand_result["rmse"]), band)
            if _finite(cand_result.get("rmse"))
            else None,
            threshold=thresholds.get("rmse_max"),
        )
    )

    # 求解器配置差异属于模型升级的一部分，记录但不阻断数值比较。
    mismatches = [
        {
            "parameter": key,
            "baseline": base_input.get(key),
            "candidate": cand_input.get(key),
        }
        for key in ("method", "max_iterations", "tolerance")
        if base_input.get(key) != cand_input.get(key)
    ]

    labels = ("isotope_d18o", "isotope_d2h", "solute_mg_l")
    predicted_rows = []
    base_predicted = base_result.get("predicted") or []
    cand_predicted = cand_result.get("predicted") or []
    for index, label in enumerate(labels):
        if index < len(base_predicted) and index < len(cand_predicted):
            predicted_rows.append(
                compare_scalar(f"predicted.{label}", base_predicted[index], cand_predicted[index])
            )

    incomparable = _sorted_incomparable(incomparable)
    summary = _build_summary(metrics, incomparable_count=len(incomparable))
    details = {
        "fractions": [_fraction_detail(row) for row in fraction_rows],
        "predicted": predicted_rows,
        "convergence": {
            "baseline": {"converged": base_result.get("converged"), "iterations": base_result.get("iterations")},
            "candidate": {"converged": cand_result.get("converged"), "iterations": cand_result.get("iterations")},
        },
    }
    return {
        "comparability": "partially_comparable" if incomparable else "comparable",
        "summary": summary,
        "details": details,
        "incomparable": incomparable,
        "parameter_mismatches": mismatches,
    }


def _fraction_detail(row: dict[str, Any]) -> dict[str, Any]:
    """明细视图保留端元身份信息，键顺序固定。"""
    return {
        "metric": row["metric"],
        "endmember": row["endmember"],
        "baseline": row["baseline"],
        "candidate": row["candidate"],
        "absolute_difference": row["absolute_difference"],
        "signed_difference": row["signed_difference"],
        "relative_difference": row["relative_difference"],
        "interval_overlap": row["interval_overlap"],
        "reason": row["reason"],
    }


def _interpolate(times: list[float], points: list[dict[str, Any]], time_days: float) -> float | None:
    """在按时间排序的浓度序列上线性插值；不做外推。"""
    if not points or time_days < times[0] - _EPS or time_days > times[-1] + _EPS:
        return None
    index = bisect_right(times, time_days)
    if index <= 0:
        return float(points[0]["concentration"])
    if index >= len(points):
        return float(points[-1]["concentration"])
    left = points[index - 1]
    right = points[index]
    span = right["time_days"] - left["time_days"]
    if abs(span) <= _EPS:
        return float(left["concentration"])
    ratio = (time_days - left["time_days"]) / span
    return float(left["concentration"]) + ratio * (float(right["concentration"]) - float(left["concentration"]))


def _build_time_axis(
    base_points: list[dict[str, Any]],
    cand_points: list[dict[str, Any]],
    options: dict[str, Any],
    base_input: dict[str, Any],
    cand_input: dict[str, Any],
) -> tuple[list[float], list[float]] | None:
    """在两条曲线的公共时间域上构造统一网格。

    默认采用两侧更细的采样步长，只在时间域交集内取值（不外推）。公共域为空
    表示时间轴不兼容，返回 None。
    """
    start = max(base_points[0]["time_days"], cand_points[0]["time_days"])
    end = min(base_points[-1]["time_days"], cand_points[-1]["time_days"])
    if end < start - _EPS:
        return None
    requested = options.get("align_step_days")
    if _finite(requested):
        step = float(requested)
    else:
        def _fallback_step(points: list[dict[str, Any]], input_data: dict[str, Any]) -> float:
            if _finite(input_data.get("step_days")):
                return float(input_data["step_days"])
            if len(points) >= 2:
                return float(points[1]["time_days"] - points[0]["time_days"])
            return 1.0

        step = float(min(_fallback_step(base_points, base_input), _fallback_step(cand_points, cand_input)))
    if step <= 0:
        return None
    axis: list[float] = []
    current = start
    while current <= end + _EPS:
        axis.append(round(current, 8))
        current += step
    return axis, [round(start, 8), round(end, 8)]


def compare_transport(
    base_input: dict[str, Any],
    base_result: dict[str, Any],
    cand_input: dict[str, Any],
    cand_result: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """比较同一井点上两个迁移模型版本的结果。"""
    thresholds = options.get("thresholds") or {}
    band = options.get("uncertainty_band")
    concentration_limit = thresholds.get("concentration_limit")
    arrival_limit = thresholds.get("arrival_time_max")

    physical_mismatch = any(
        not _close(base_input.get(key), cand_input.get(key))
        for key in _TRANSPORT_PHYSICAL_PARAMS
    )
    mismatches = [
        {"parameter": key, "baseline": base_input.get(key), "candidate": cand_input.get(key)}
        for key in (*_TRANSPORT_PHYSICAL_PARAMS, *_TRANSPORT_GRID_PARAMS)
        if not _close(base_input.get(key), cand_input.get(key))
    ]

    base_points = sorted(base_result.get("points", []), key=lambda p: p["time_days"])
    cand_points = sorted(cand_result.get("points", []), key=lambda p: p["time_days"])
    base_times = [p["time_days"] for p in base_points]
    cand_times = [p["time_days"] for p in cand_points]

    incomparable: list[dict[str, Any]] = []
    axis_info: list[float] | None = None
    series_rows: list[dict[str, Any]] = []

    aligned = None
    # 物理参数不一致时曲线差异不能归因于模型版本，直接标记不可比、跳过逐点计算。
    if base_points and cand_points and not physical_mismatch:
        aligned = _build_time_axis(base_points, cand_points, options, base_input, cand_input)
    if aligned is None and base_points and cand_points:
        reason = "physical_parameter_mismatch" if physical_mismatch else "non_overlapping_time_axis"
        incomparable.append(
            {
                "field": "concentration_curve",
                "reason": reason,
                "detail": {
                    "baseline_domain": [base_points[0]["time_days"], base_points[-1]["time_days"]],
                    "candidate_domain": [cand_points[0]["time_days"], cand_points[-1]["time_days"]],
                }
                if not physical_mismatch
                else {},
            }
        )

    metrics: list[dict[str, Any]] = []

    def scalar_with_physical_guard(metric: str, baseline: Any, candidate: Any, **kwargs: Any) -> dict[str, Any]:
        if physical_mismatch:
            return compare_scalar(
                metric, baseline, candidate, incomparable_reason="physical_parameter_mismatch", **kwargs
            )
        return compare_scalar(metric, baseline, candidate, **kwargs)

    base_arrival = base_result.get("arrival_time_days")
    cand_arrival = cand_result.get("arrival_time_days")
    metrics.append(
        scalar_with_physical_guard(
            "arrival_time_days",
            base_arrival,
            cand_arrival,
            baseline_interval=_relative_interval(float(base_arrival), band)
            if _finite(base_arrival)
            else None,
            candidate_interval=_relative_interval(float(cand_arrival), band)
            if _finite(cand_arrival)
            else None,
            threshold=arrival_limit,
        )
    )

    base_peak = (base_result.get("peak") or {}).get("concentration")
    cand_peak = (cand_result.get("peak") or {}).get("concentration")
    metrics.append(
        scalar_with_physical_guard(
            "peak_concentration",
            base_peak,
            cand_peak,
            baseline_interval=_relative_interval(float(base_peak), band) if _finite(base_peak) else None,
            candidate_interval=_relative_interval(float(cand_peak), band) if _finite(cand_peak) else None,
            threshold=concentration_limit,
        )
    )

    first_exceedance = None
    curve_max: dict[str, Any] | None = None
    if aligned is not None:
        axis, domain = aligned
        axis_info = domain
        max_abs = 0.0
        max_abs_time: float | None = None
        first_base = first_cand = None
        for time_days in axis:
            base_value = _interpolate(base_times, base_points, time_days)
            cand_value = _interpolate(cand_times, cand_points, time_days)
            if base_value is None or cand_value is None:
                continue
            row = compare_scalar(
                "concentration",
                base_value,
                cand_value,
                baseline_interval=_relative_interval(base_value, band),
                candidate_interval=_relative_interval(cand_value, band),
            )
            row["time_days"] = time_days
            series_rows.append(row)
            if row["absolute_difference"] is not None and row["absolute_difference"] > max_abs:
                max_abs = row["absolute_difference"]
                max_abs_time = time_days
            if concentration_limit is not None and not physical_mismatch:
                if first_base is None and base_value > float(concentration_limit) + _EPS:
                    first_base = time_days
                if first_cand is None and cand_value > float(concentration_limit) + _EPS:
                    first_cand = time_days

        if not physical_mismatch:
            curve_max = {
                "metric": "curve_max_absolute_difference",
                "absolute_difference": max_abs,
                "time_days": max_abs_time,
            }

        if concentration_limit is not None:
            if physical_mismatch:
                incomparable.append(
                    {
                        "field": "first_exceedance_time",
                        "reason": "physical_parameter_mismatch",
                        "detail": {"limit": concentration_limit},
                    }
                )
            else:
                if first_base is None and first_cand is None:
                    change = "stays_below_limit"
                elif first_base is None:
                    change = "new_exceedance"
                elif first_cand is None:
                    change = "exceedance_cleared"
                else:
                    change = "earlier" if first_cand < first_base - _EPS else (
                        "later" if first_cand > first_base + _EPS else "unchanged"
                    )
                first_exceedance = {
                    "limit": concentration_limit,
                    "baseline_time_days": first_base,
                    "candidate_time_days": first_cand,
                    "signed_difference_days": (
                        None if first_base is None or first_cand is None else first_cand - first_base
                    ),
                    "change": change,
                }
    else:
        if not any(item["field"] == "concentration_curve" for item in incomparable):
            incomparable.append(
                {"field": "concentration_curve", "reason": "missing_curve_points"}
            )

    if physical_mismatch:
        for metric_name in ("arrival_time_days", "peak_concentration"):
            incomparable.append(
                {"field": metric_name, "reason": "physical_parameter_mismatch"}
            )
        incomparable.append(
            {"field": "concentration_curve", "reason": "physical_parameter_mismatch"}
        )

    # 去重并稳定排序不可比项。
    incomparable = _dedupe_incomparable(incomparable + [
        {
            "field": row["metric"],
            "reason": row["reason"],
        }
        for row in metrics
        if not row["comparable"] and row["reason"] != "physical_parameter_mismatch"
    ])

    summary = _build_summary(metrics, incomparable_count=len(incomparable))
    summary["first_exceedance"] = first_exceedance
    summary["curve_max_difference"] = curve_max
    details = {
        "time_axis": {
            "aligned_step_days": (
                options.get("align_step_days")
                if _finite(options.get("align_step_days"))
                else (
                    min(base_input.get("step_days", 0.0), cand_input.get("step_days", 0.0))
                    or None
                )
            ),
            "common_domain": axis_info,
            "point_count": len(series_rows),
        },
        "peak": {
            "baseline": base_result.get("peak"),
            "candidate": cand_result.get("peak"),
        },
        "series": [
            {
                "time_days": row["time_days"],
                "baseline": row["baseline"],
                "candidate": row["candidate"],
                "absolute_difference": row["absolute_difference"],
                "signed_difference": row["signed_difference"],
                "relative_difference": row["relative_difference"],
                "interval_overlap": row["interval_overlap"],
                "reason": row["reason"],
            }
            for row in series_rows
        ],
    }
    return {
        "comparability": "partially_comparable" if incomparable else "comparable",
        "summary": summary,
        "details": details,
        "incomparable": _sorted_incomparable(incomparable),
        "parameter_mismatches": mismatches,
    }


def _close(a: Any, b: Any, rel_tol: float = 1e-9) -> bool:
    if a == b:
        return True
    if _finite(a) and _finite(b):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=_EPS)
    return False


def _build_summary(
    metrics: list[dict[str, Any]], *, incomparable_count: int | None = None
) -> dict[str, Any]:
    """从有序指标行构造摘要，统计可比数量并给出变化最大的指标。

    不同指标量纲不同（比例、残差、浓度、天），"最大变化"优先按无量纲的相对差
    幅度排序；相对差缺失（如基线为 0）时回退到绝对差。``incomparable_count``
    取报告级不可比字段数（可能包含不在指标行内的字段，如整条曲线、单侧端元）。
    """
    comparable_rows = [row for row in metrics if row["comparable"]]

    def _magnitude(row: dict[str, Any]) -> tuple[int, float, float]:
        rel = row["relative_difference"]
        if rel is not None:
            return (1, abs(rel), row["absolute_difference"] or 0.0)
        return (0, row["absolute_difference"] if row["absolute_difference"] is not None else -1.0, 0.0)

    largest = max(comparable_rows, key=_magnitude, default=None)
    crossings = [
        {"metric": row["metric"], **row["threshold_crossing"]}
        for row in metrics
        if row.get("threshold_crossing")
    ]
    return {
        "metric_count": len(metrics),
        "comparable_count": len(comparable_rows),
        "incomparable_count": (
            len(metrics) - len(comparable_rows) if incomparable_count is None else incomparable_count
        ),
        "largest_change": (
            {
                "metric": largest["metric"],
                "absolute_difference": largest["absolute_difference"],
                "relative_difference": largest["relative_difference"],
            }
            if largest is not None
            else None
        ),
        "threshold_crossings": crossings,
        "metrics": metrics,
    }


def _dedupe_incomparable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (item["field"], item["reason"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _sorted_incomparable(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """不可比字段按 (field, reason) 稳定排序，保证输出确定。"""
    return sorted(
        _dedupe_incomparable(items),
        key=lambda item: (item["field"], item["reason"]),
    )
