"""标准曲线换算：斜率受限的加权最小二乘拟合与待测信号反算。

拟合问题：给定按浓度递增的 n 个标准点 (x_i, y_i)（权重 w_i 为正整数，
2 <= n <= 12），求拟合节点 f_1..f_n，使加权信号残差平方和

    sum w_i * (f_i - y_i)^2

最小，且满足

    0 <= f_{i+1} - f_i <= s * (x_{i+1} - x_i)

其中 s 为正斜率上限。目标函数严格凸（w_i > 0），解唯一。这里用原始积极集
法（primal active-set）求解该凸二次规划，全程使用 Fraction 精确有理数运
算，不存在浮点舍入；拟合节点以最简分数返回。

反算：拟合曲线为节点间的分段线性函数（连续、不降）。待测信号 t 的完整浓
度闭区间 [lo, hi] 是曲线恰好取到 t 的全部浓度；平台段（相邻拟合值相等）
整段保留。t 超出拟合信号范围 [f_1, f_n] 时不外推，标记为无法换算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Sequence

MIN_STANDARDS = 2
MAX_STANDARDS = 12
MAX_UNKNOWNS = 30
MAX_WEIGHT = 1_000_000
MAX_CONCENTRATION = 999_999_999
_MAX_DECIMAL_LENGTH = 64
_MAX_DECIMAL_EXPONENT = 999
_MAX_QP_ITERATIONS = 10_000

_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?$")


@dataclass(frozen=True, slots=True)
class CurveIssue:
    """一处非法输入；loc 为 JSON 路径（字段名与数组下标）。"""

    loc: tuple[str | int, ...]
    message: str


class CurveValidationError(ValueError):
    """请求体不符合标准曲线换算的输入契约。"""

    def __init__(self, message: str, issues: Sequence[CurveIssue]):
        super().__init__(message)
        self.message = message
        self.issues = tuple(issues)


@dataclass(frozen=True, slots=True)
class StandardPoint:
    concentration: int
    signal: Fraction
    signal_raw: str
    weight: int


@dataclass(frozen=True, slots=True)
class UnknownSignal:
    raw: str
    value: Fraction


@dataclass(frozen=True, slots=True)
class CurveRequest:
    """已通过校验的请求；standards 按浓度严格递增排列。"""

    standards: tuple[StandardPoint, ...]
    slope_limit: Fraction
    unknown_signals: tuple[UnknownSignal, ...]


def _parse_decimal(value: object, loc: tuple[str | int, ...], issues: list[CurveIssue]) -> Fraction | None:
    if not isinstance(value, str):
        issues.append(CurveIssue(loc, '必须是十进制字符串，例如 "0.125"。'))
        return None
    if not value or len(value) > _MAX_DECIMAL_LENGTH:
        issues.append(CurveIssue(loc, f"十进制字符串长度必须在 1~{_MAX_DECIMAL_LENGTH} 个字符之间。"))
        return None
    match = _DECIMAL_RE.match(value)
    if match is None:
        issues.append(
            CurveIssue(loc, f"不是有效的十进制字符串：{value!r}。允许形如 12、-3.5、0.125、1e-3。")
        )
        return None
    exponent = match.group(1)
    if exponent is not None and abs(int(exponent)) > _MAX_DECIMAL_EXPONENT:
        issues.append(CurveIssue(loc, f"十进制指数超出 ±{_MAX_DECIMAL_EXPONENT}：{value!r}。"))
        return None
    try:
        return Fraction(Decimal(value))
    except InvalidOperation:  # pragma: no cover - 正则已排除，仅作防御
        issues.append(CurveIssue(loc, f"不是有效的十进制字符串：{value!r}。"))
        return None


def _parse_int(
    value: object,
    loc: tuple[str | int, ...],
    issues: list[CurveIssue],
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    # bool 是 int 的子类，必须显式排除，避免 true 被当成 1。
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(CurveIssue(loc, f"{name}必须是整数（JSON 数字，不能是字符串、布尔或小数）。"))
        return None
    if not minimum <= value <= maximum:
        issues.append(CurveIssue(loc, f"{name}必须在 {minimum}~{maximum} 之间，实际为 {value}。"))
        return None
    return value


def parse_curve_request(payload: object) -> CurveRequest:
    """校验原始 JSON 对象并返回排序后的领域请求；非法时抛出带定位的异常。"""

    if not isinstance(payload, dict):
        raise CurveValidationError(
            "请求体必须是 JSON 对象。",
            (CurveIssue((), "请求体必须是 JSON 对象，包含 standards、slope_limit、unknown_signals。"),),
        )

    issues: list[CurveIssue] = []
    for field in ("standards", "slope_limit", "unknown_signals"):
        if field not in payload:
            issues.append(CurveIssue((field,), "缺少必需字段。"))

    standards: list[StandardPoint] = []
    raw_standards = payload.get("standards")
    if isinstance(raw_standards, list):
        if not MIN_STANDARDS <= len(raw_standards) <= MAX_STANDARDS:
            issues.append(
                CurveIssue(
                    ("standards",),
                    f"标准点数量必须在 {MIN_STANDARDS}~{MAX_STANDARDS} 之间，实际为 {len(raw_standards)}。",
                )
            )
        else:
            seen: dict[int, int] = {}
            for index, item in enumerate(raw_standards):
                if not isinstance(item, dict):
                    issues.append(CurveIssue(("standards", index), "每个标准点必须是对象。"))
                    continue
                for field in ("concentration", "signal", "weight"):
                    if field not in item:
                        issues.append(CurveIssue(("standards", index, field), "缺少必需字段。"))

                concentration = _parse_int(
                    item.get("concentration"),
                    ("standards", index, "concentration"),
                    issues,
                    name="浓度",
                    minimum=0,
                    maximum=MAX_CONCENTRATION,
                )
                weight = _parse_int(
                    item.get("weight"),
                    ("standards", index, "weight"),
                    issues,
                    name="权重",
                    minimum=1,
                    maximum=MAX_WEIGHT,
                )
                signal_raw = item.get("signal")
                signal = _parse_decimal(signal_raw, ("standards", index, "signal"), issues)

                if concentration is not None:
                    if concentration in seen:
                        issues.append(
                            CurveIssue(
                                ("standards", index, "concentration"),
                                f"浓度 {concentration} 与第 {seen[concentration]} 个标准点重复；浓度必须互异。",
                            )
                        )
                    else:
                        seen[concentration] = index

                if concentration is not None and weight is not None and signal is not None:
                    standards.append(
                        StandardPoint(
                            concentration=concentration,
                            signal=signal,
                            signal_raw=signal_raw if isinstance(signal_raw, str) else "",
                            weight=weight,
                        )
                    )
    elif "standards" in payload:
        issues.append(CurveIssue(("standards",), "必须是标准点数组。"))

    slope_limit: Fraction | None = None
    if "slope_limit" in payload:
        slope_limit = _parse_decimal(payload.get("slope_limit"), ("slope_limit",), issues)
        if slope_limit is not None and slope_limit <= 0:
            issues.append(
                CurveIssue(("slope_limit",), f"斜率上限必须是正数，实际为 {payload.get('slope_limit')!r}。")
            )
            slope_limit = None

    unknowns: list[UnknownSignal] = []
    raw_unknowns = payload.get("unknown_signals")
    if isinstance(raw_unknowns, list):
        if len(raw_unknowns) > MAX_UNKNOWNS:
            issues.append(
                CurveIssue(
                    ("unknown_signals",),
                    f"待测信号最多 {MAX_UNKNOWNS} 个，实际为 {len(raw_unknowns)}。",
                )
            )
        else:
            for index, item in enumerate(raw_unknowns):
                value = _parse_decimal(item, ("unknown_signals", index), issues)
                if value is not None:
                    unknowns.append(UnknownSignal(raw=item, value=value))
    elif "unknown_signals" in payload:
        issues.append(CurveIssue(("unknown_signals",), "必须是十进制字符串数组。"))

    if issues:
        raise CurveValidationError(f"请求存在 {len(issues)} 处非法输入。", issues)

    standards.sort(key=lambda point: point.concentration)
    return CurveRequest(
        standards=tuple(standards),
        slope_limit=slope_limit,  # type: ignore[arg-type]
        unknown_signals=tuple(unknowns),
    )


def _solve_linear_system(matrix: list[list[Fraction]], rhs: list[Fraction]) -> list[Fraction]:
    """Gauss-Jordan 精确消元；矩阵奇异时抛 RuntimeError。"""

    size = len(matrix)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = next((r for r in range(col, size) if aug[r][col] != 0), None)
        if pivot is None:
            raise RuntimeError("线性系统奇异，无法精确求解。")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_value = aug[col][col]
        aug[col] = [value / pivot_value for value in aug[col]]
        for row in range(size):
            if row != col and aug[row][col] != 0:
                factor = aug[row][col]
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [aug[i][size] for i in range(size)]


def _eqp_step(
    weights: list[Fraction],
    signals: list[Fraction],
    current: list[Fraction],
    active_normals: list[list[Fraction]],
) -> tuple[list[Fraction], list[Fraction]]:
    """解等式约束子问题：min q(current+p) s.t. A p = 0，返回 (p, 乘子)。"""

    n = len(weights)
    k = len(active_normals)
    size = n + k
    matrix = [[Fraction(0)] * size for _ in range(size)]
    rhs = [Fraction(0)] * size
    for i in range(n):
        matrix[i][i] = 2 * weights[i]
        rhs[i] = -2 * weights[i] * (current[i] - signals[i])
    for j, normal in enumerate(active_normals):
        for i in range(n):
            if normal[i]:
                matrix[i][n + j] = normal[i]
                matrix[n + j][i] = normal[i]
    solution = _solve_linear_system(matrix, rhs)
    return solution[:n], solution[n:]


@dataclass(frozen=True, slots=True)
class FittedCurve:
    """拟合结果：浓度节点（严格递增整数）与对应拟合信号（最简分数）。"""

    concentrations: tuple[int, ...]
    fitted: tuple[Fraction, ...]
    weighted_residual_sum_squares: Fraction

    def invert(self, signal: Fraction) -> tuple[Fraction, Fraction] | None:
        """返回信号对应的完整浓度闭区间；超出拟合信号范围时返回 None。

        曲线连续且不降，因此原像是闭区间 [lo, hi]；平台段（相邻拟合值相等）
        的两端分别落在平台的首尾节点上，整段保留。
        """

        xs = self.concentrations
        fs = self.fitted
        last = len(xs) - 1
        if signal < fs[0] or signal > fs[last]:
            return None

        if signal == fs[0]:
            lower = Fraction(xs[0])
        else:
            lower = None
            for i in range(last):
                if fs[i] < signal <= fs[i + 1]:
                    lower = Fraction(xs[i]) + (signal - fs[i]) * Fraction(xs[i + 1] - xs[i]) / (
                        fs[i + 1] - fs[i]
                    )
                    break
            assert lower is not None  # signal <= fs[last] 保证存在

        if signal == fs[last]:
            upper = Fraction(xs[last])
        else:
            upper = None
            for i in range(last - 1, -1, -1):
                if fs[i] <= signal < fs[i + 1]:
                    upper = Fraction(xs[i]) + (signal - fs[i]) * Fraction(xs[i + 1] - xs[i]) / (
                        fs[i + 1] - fs[i]
                    )
                    break
            assert upper is not None  # signal >= fs[0] 保证存在

        return lower, upper


def fit_curve(standards: Sequence[StandardPoint], slope_limit: Fraction) -> FittedCurve:
    """原始积极集法精确求解斜率受限加权最小二乘拟合。

    约束（对每对相邻节点 i）：
      L_i: f_i - f_{i+1} <= 0            （拟合节点不降）
      U_i: f_{i+1} - f_i <= s * (x_{i+1} - x_i)   （增量不超过斜率上限乘浓度差）
    """

    n = len(standards)
    xs = [Fraction(point.concentration) for point in standards]
    ys = [point.signal for point in standards]
    ws = [Fraction(point.weight) for point in standards]

    constraints: list[tuple[list[Fraction], Fraction]] = []
    for i in range(n - 1):
        lower = [Fraction(0)] * n
        lower[i] = Fraction(1)
        lower[i + 1] = Fraction(-1)
        constraints.append((lower, Fraction(0)))
        upper = [Fraction(0)] * n
        upper[i] = Fraction(-1)
        upper[i + 1] = Fraction(1)
        constraints.append((upper, slope_limit * (xs[i + 1] - xs[i])))

    # 任意常数向量均可行（约束只限制相邻差），取加权平均作为起点。
    start = sum(w * y for w, y in zip(ws, ys)) / sum(ws)
    current = [start] * n
    active: list[int] = []

    for _ in range(_MAX_QP_ITERATIONS):
        normals = [constraints[j][0] for j in active]
        direction, multipliers = _eqp_step(ws, ys, current, normals)

        if all(step == 0 for step in direction):
            # 当前点是该工作集下的最优；检查 KKT 乘子符号。
            drop_position = None
            worst = Fraction(0)
            for position, multiplier in enumerate(multipliers):
                if multiplier < worst:
                    worst = multiplier
                    drop_position = position
            if drop_position is None:
                residual = sum(w * (f - y) ** 2 for w, f, y in zip(ws, current, ys))
                return FittedCurve(
                    concentrations=tuple(point.concentration for point in standards),
                    fitted=tuple(current),
                    weighted_residual_sum_squares=residual,
                )
            active.pop(drop_position)
            continue

        # 沿方向前进，碰到首个约束则将其加入工作集。
        step = Fraction(1)
        blocker: int | None = None
        active_set = set(active)
        for j, (normal, bound) in enumerate(constraints):
            if j in active_set:
                continue
            denom = sum(a * d for a, d in zip(normal, direction))
            if denom > 0:
                headroom = bound - sum(a * c for a, c in zip(normal, current))
                candidate = headroom / denom
                if candidate < step:
                    step = candidate
                    blocker = j
        current = [c + step * d for c, d in zip(current, direction)]
        if blocker is not None:
            active.append(blocker)
            active.sort()

    raise RuntimeError("积极集法未在限定迭代次数内收敛。")  # pragma: no cover - 防御性


def format_fraction(value: Fraction) -> str:
    """最简分数形式：分母为 1 时只输出整数。"""

    return str(value)
