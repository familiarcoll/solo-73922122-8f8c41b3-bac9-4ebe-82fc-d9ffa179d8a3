"""标准曲线拟合与反算的精确性测试。

拟合结果通过与穷举积极集的暴力参考解（同样使用 Fraction 精确运算）逐项比对，
保证积极集法在随机输入下与全局最优完全一致。
"""

from __future__ import annotations

import random
from fractions import Fraction
from itertools import combinations

import pytest

from app.curve import (
    CurveValidationError,
    StandardPoint,
    _solve_linear_system,
    fit_curve,
    parse_curve_request,
)


def _point(concentration: int, signal: str, weight: int = 1) -> StandardPoint:
    return StandardPoint(
        concentration=concentration,
        signal=Fraction(signal),
        signal_raw=signal,
        weight=weight,
    )


def _brute_force_fit(
    concentrations: list[int],
    signals: list[Fraction],
    weights: list[Fraction],
    slope_limit: Fraction,
) -> tuple[Fraction, ...]:
    """穷举所有积极约束集合，返回目标最小的可行 KKT 驻点（精确解）。"""

    n = len(concentrations)
    constraints: list[tuple[list[Fraction], Fraction]] = []
    for i in range(n - 1):
        lower = [Fraction(0)] * n
        lower[i], lower[i + 1] = Fraction(1), Fraction(-1)
        constraints.append((lower, Fraction(0)))
        upper = [Fraction(0)] * n
        upper[i], upper[i + 1] = Fraction(-1), Fraction(1)
        constraints.append((upper, slope_limit * (concentrations[i + 1] - concentrations[i])))

    best_f: tuple[Fraction, ...] | None = None
    best_obj: Fraction | None = None
    for size in range(len(constraints) + 1):
        for subset in combinations(range(len(constraints)), size):
            normals = [constraints[j][0] for j in subset]
            k = len(normals)
            dim = n + k
            matrix = [[Fraction(0)] * dim for _ in range(dim)]
            rhs = [Fraction(0)] * dim
            for i in range(n):
                matrix[i][i] = 2 * weights[i]
                rhs[i] = 2 * weights[i] * signals[i]
            for j, normal in enumerate(normals):
                for i in range(n):
                    matrix[i][n + j] = normal[i]
                    matrix[n + j][i] = normal[i]
                rhs[n + j] = constraints[subset[j]][1]
            try:
                solution = _solve_linear_system(matrix, rhs)
            except RuntimeError:
                continue  # 法向量线性相关，跳过
            f, multipliers = solution[:n], solution[n:]
            if any(multiplier < 0 for multiplier in multipliers):
                continue
            if any(
                sum(a * fi for a, fi in zip(normal, f)) > bound for normal, bound in constraints
            ):
                continue
            objective = sum(w * (fi - yi) ** 2 for w, fi, yi in zip(weights, f, signals))
            if best_obj is None or objective < best_obj:
                best_obj = objective
                best_f = tuple(f)
    assert best_f is not None
    return best_f


# ---------------------------------------------------------------- 输入校验


def _payload(**overrides):
    payload = {
        "standards": [
            {"concentration": 0, "signal": "0.010", "weight": 1},
            {"concentration": 5, "signal": "0.500", "weight": 2},
        ],
        "slope_limit": "0.25",
        "unknown_signals": ["0.3"],
    }
    payload.update(overrides)
    return payload


def test_valid_request_is_sorted_by_concentration() -> None:
    request = parse_curve_request(
        _payload(
            standards=[
                {"concentration": 5, "signal": "0.500", "weight": 2},
                {"concentration": 0, "signal": "0.010", "weight": 1},
            ]
        )
    )
    assert [p.concentration for p in request.standards] == [0, 5]
    assert request.standards[0].signal == Fraction(1, 100)
    assert request.standards[1].weight == 2
    assert request.slope_limit == Fraction(1, 4)
    assert request.unknown_signals[0].value == Fraction(3, 10)


@pytest.mark.parametrize(
    ("standards", "loc"),
    [
        ([], ("standards",)),
        ([{"concentration": 0, "signal": "0.1", "weight": 1}], ("standards",)),
        (
            [{"concentration": i, "signal": "0.1", "weight": 1} for i in range(13)],
            ("standards",),
        ),
    ],
)
def test_standard_count_limits(standards, loc) -> None:
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    assert excinfo.value.issues[0].loc == loc


@pytest.mark.parametrize(
    ("concentration", "fragment"),
    [
        (True, "整数"),
        ("3", "整数"),
        (1.5, "整数"),
        (-1, "之间"),
        (10**10, "之间"),
    ],
)
def test_concentration_must_be_nonnegative_int(concentration, fragment) -> None:
    standards = [
        {"concentration": 0, "signal": "0.1", "weight": 1},
        {"concentration": concentration, "signal": "0.2", "weight": 1},
    ]
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    issue = excinfo.value.issues[0]
    assert issue.loc == ("standards", 1, "concentration")
    assert fragment in issue.message


def test_duplicate_concentration_is_located() -> None:
    standards = [
        {"concentration": 2, "signal": "0.1", "weight": 1},
        {"concentration": 2, "signal": "0.2", "weight": 1},
    ]
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    issue = excinfo.value.issues[0]
    assert issue.loc == ("standards", 1, "concentration")
    assert "互异" in issue.message


@pytest.mark.parametrize(
    ("weight", "fragment"),
    [
        (0, "之间"),
        (-3, "之间"),
        (True, "整数"),
        (2.0, "整数"),
        ("2", "整数"),
        (1_000_001, "之间"),
    ],
)
def test_weight_must_be_positive_int(weight, fragment) -> None:
    standards = [
        {"concentration": 0, "signal": "0.1", "weight": 1},
        {"concentration": 1, "signal": "0.2", "weight": weight},
    ]
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    issue = excinfo.value.issues[0]
    assert issue.loc == ("standards", 1, "weight")
    assert fragment in issue.message


@pytest.mark.parametrize(
    "signal",
    ["", "abc", "1,5", "NaN", "Infinity", "0x10", "1e1000", "1 2", "0.1 "],
)
def test_signal_must_be_strict_decimal_string(signal) -> None:
    standards = [
        {"concentration": 0, "signal": "0.1", "weight": 1},
        {"concentration": 1, "signal": signal, "weight": 1},
    ]
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    assert excinfo.value.issues[0].loc == ("standards", 1, "signal")


def test_signal_must_be_string_not_number() -> None:
    standards = [
        {"concentration": 0, "signal": 0.1, "weight": 1},
        {"concentration": 1, "signal": "0.2", "weight": 1},
    ]
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(standards=standards))
    assert excinfo.value.issues[0].loc == ("standards", 0, "signal")


@pytest.mark.parametrize("slope_limit", ["0", "-0.5", "0.0"])
def test_slope_limit_must_be_positive(slope_limit) -> None:
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(slope_limit=slope_limit))
    assert excinfo.value.issues[0].loc == ("slope_limit",)


def test_unknown_signals_limit_and_item_location() -> None:
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(unknown_signals=["0.1"] * 31))
    assert excinfo.value.issues[0].loc == ("unknown_signals",)

    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request(_payload(unknown_signals=["0.1", "oops", 5]))
    locs = {issue.loc for issue in excinfo.value.issues}
    assert ("unknown_signals", 1) in locs
    assert ("unknown_signals", 2) in locs


def test_missing_fields_are_all_reported() -> None:
    with pytest.raises(CurveValidationError) as excinfo:
        parse_curve_request({})
    locs = {issue.loc for issue in excinfo.value.issues}
    assert ("standards",) in locs
    assert ("slope_limit",) in locs
    assert ("unknown_signals",) in locs


def test_non_object_body_rejected() -> None:
    with pytest.raises(CurveValidationError):
        parse_curve_request([1, 2, 3])


def test_decimal_exponent_forms_accepted() -> None:
    request = parse_curve_request(
        _payload(
            standards=[
                {"concentration": 0, "signal": "1e-3", "weight": 1},
                {"concentration": 1, "signal": "2.5E+1", "weight": 1},
            ],
            slope_limit="3e1",
        )
    )
    assert request.standards[0].signal == Fraction(1, 1000)
    assert request.standards[1].signal == Fraction(25)
    assert request.slope_limit == Fraction(30)


# ---------------------------------------------------------------- 拟合


def test_unconstrained_fit_equals_observations() -> None:
    points = [_point(0, "0.10"), _point(2, "0.30"), _point(7, "0.90")]
    curve = fit_curve(points, Fraction(5))
    assert curve.fitted == (Fraction(1, 10), Fraction(3, 10), Fraction(9, 10))
    assert curve.weighted_residual_sum_squares == 0


def test_plateau_pooling_with_weights() -> None:
    # 与 /docs 平台段示例相同：前三个点被池化为 19/20 的平台。
    points = [
        _point(0, "1.00", 1),
        _point(1, "0.90", 1),
        _point(2, "0.95", 2),
        _point(3, "2.00", 1),
        _point(4, "2.10", 1),
    ]
    curve = fit_curve(points, Fraction(5))
    assert curve.fitted == (
        Fraction(19, 20),
        Fraction(19, 20),
        Fraction(19, 20),
        Fraction(2),
        Fraction(21, 10),
    )
    assert curve.weighted_residual_sum_squares == Fraction(1, 200)


def test_slope_limit_caps_rise() -> None:
    # 与 /docs 斜率受限示例相同。
    points = [
        _point(0, "0.010"),
        _point(1, "0.040"),
        _point(2, "0.900"),
        _point(3, "1.200"),
        _point(4, "1.100"),
    ]
    curve = fit_curve(points, Fraction(1, 4))
    assert curve.fitted == (
        Fraction(13, 80),
        Fraction(33, 80),
        Fraction(53, 80),
        Fraction(73, 80),
        Fraction(11, 10),
    )
    assert curve.weighted_residual_sum_squares == Fraction(12043, 40000)


def test_two_points_weighted_pooling() -> None:
    points = [_point(0, "1", 1), _point(3, "0", 3)]
    curve = fit_curve(points, Fraction(10))
    assert curve.fitted == (Fraction(1, 4), Fraction(1, 4))


def test_random_inputs_match_brute_force() -> None:
    rng = random.Random(20261111)
    for _ in range(80):
        n = rng.randint(2, 4)
        concentrations = sorted(rng.sample(range(0, 15), n))
        signals = [Fraction(rng.randint(-100, 300), 100) for _ in range(n)]
        weights = [Fraction(rng.randint(1, 5)) for _ in range(n)]
        slope = Fraction(rng.randint(1, 40), 10)
        points = [
            StandardPoint(c, s, str(s), int(w))
            for c, s, w in zip(concentrations, signals, weights)
        ]
        curve = fit_curve(points, slope)
        expected = _brute_force_fit(concentrations, signals, weights, slope)
        assert curve.fitted == expected, (concentrations, signals, weights, slope)


def test_larger_random_inputs_match_brute_force() -> None:
    rng = random.Random(7)
    for _ in range(12):
        n = 5
        concentrations = sorted(rng.sample(range(0, 30), n))
        signals = [Fraction(rng.randint(0, 500), 100) for _ in range(n)]
        weights = [Fraction(rng.randint(1, 4)) for _ in range(n)]
        slope = Fraction(rng.randint(1, 30), 10)
        points = [
            StandardPoint(c, s, str(s), int(w))
            for c, s, w in zip(concentrations, signals, weights)
        ]
        curve = fit_curve(points, slope)
        expected = _brute_force_fit(concentrations, signals, weights, slope)
        assert curve.fitted == expected


def test_twelve_points_fit_quickly() -> None:
    rng = random.Random(99)
    concentrations = list(range(12))
    signals = [Fraction(rng.randint(0, 1000), 100) for _ in range(12)]
    points = [StandardPoint(c, s, str(s), 1) for c, s in zip(concentrations, signals)]
    curve = fit_curve(points, Fraction(3, 10))
    # 约束必须满足：不降且增量受限。
    for i in range(11):
        assert curve.fitted[i + 1] >= curve.fitted[i]
        assert curve.fitted[i + 1] - curve.fitted[i] <= Fraction(3, 10)


# ---------------------------------------------------------------- 反算


def _curve(fitted: list[str], concentrations: list[int] | None = None):
    n = len(fitted)
    concentrations = concentrations or list(range(n))
    points = [
        StandardPoint(c, Fraction(f), f, 1) for c, f in zip(concentrations, fitted)
    ]
    return fit_curve(points, Fraction(10**6))


def test_invert_interpolates_exact_fraction() -> None:
    curve = _curve(["0.2", "0.45", "0.7"])
    assert curve.invert(Fraction(3, 10)) == (Fraction(2, 5), Fraction(2, 5))


def test_invert_returns_whole_plateau() -> None:
    curve = _curve(["0.5", "0.5", "0.5", "2"])
    assert curve.invert(Fraction(1, 2)) == (Fraction(0), Fraction(2))


def test_invert_endpoints_and_nodes() -> None:
    curve = _curve(["0.2", "0.45", "0.7"])
    assert curve.invert(Fraction(1, 5)) == (Fraction(0), Fraction(0))
    assert curve.invert(Fraction(7, 10)) == (Fraction(2), Fraction(2))
    assert curve.invert(Fraction(9, 20)) == (Fraction(1), Fraction(1))


def test_invert_out_of_range() -> None:
    curve = _curve(["0.2", "0.45", "0.7"])
    assert curve.invert(Fraction(1, 5) - Fraction(1, 1000)) is None
    assert curve.invert(Fraction(7, 10) + Fraction(1, 1000)) is None
    assert curve.invert(Fraction(-1)) is None


def test_invert_flat_curve_covers_whole_range() -> None:
    curve = _curve(["1", "1", "1"])
    assert curve.invert(Fraction(1)) == (Fraction(0), Fraction(2))
    assert curve.invert(Fraction(2)) is None


def test_invert_uses_actual_concentrations() -> None:
    curve = _curve(["0", "1"], concentrations=[10, 40])
    assert curve.invert(Fraction(1, 2)) == (Fraction(25), Fraction(25))
