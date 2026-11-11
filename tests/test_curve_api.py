"""标准曲线换算 API 的端到端测试（含 /docs 内置示例的可运行性）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

DEMO = Path(__file__).resolve().parent.parent / "demo"
client = TestClient(app)

URL = "/v1/standard-curve-conversions"


def _post(payload: object):
    return client.post(URL, json=payload)


def test_plateau_example_end_to_end() -> None:
    payload = json.loads((DEMO / "curve_plateau.json").read_text(encoding="utf-8"))
    response = _post(payload)
    assert response.status_code == 200
    body = response.json()

    assert body["summary"] == {
        "standard_count": 5,
        "unknown_count": 3,
        "slope_limit": "5",
        "min_concentration": 0,
        "max_concentration": 4,
        "min_fitted_signal": "19/20",
        "max_fitted_signal": "21/10",
        "weighted_residual_sum_squares": "1/200",
    }
    assert [node["fitted_signal"] for node in body["fitted_nodes"]] == [
        "19/20",
        "19/20",
        "19/20",
        "2",
        "21/10",
    ]
    assert body["fitted_nodes"][0]["observed_signal"] == "1.00"
    assert body["fitted_nodes"][2]["weight"] == 2

    conversions = {item["signal"]: item for item in body["conversions"]}
    # 命中平台：整段保留。
    assert conversions["0.95"]["status"] == "ok"
    assert conversions["0.95"]["concentration_interval"] == {"lower": "0", "upper": "2"}
    # 线性插值，精确分数。
    assert conversions["1.50"]["concentration_interval"] == {
        "lower": "53/21",
        "upper": "53/21",
    }
    # 低于拟合信号范围：不外推。
    assert conversions["0.50"]["status"] == "out_of_range"
    assert conversions["0.50"]["concentration_interval"] is None


def test_slope_limited_example_end_to_end() -> None:
    payload = json.loads((DEMO / "curve_slope_limited.json").read_text(encoding="utf-8"))
    response = _post(payload)
    assert response.status_code == 200
    body = response.json()

    assert [node["fitted_signal"] for node in body["fitted_nodes"]] == [
        "13/80",
        "33/80",
        "53/80",
        "73/80",
        "11/10",
    ]
    assert body["summary"]["weighted_residual_sum_squares"] == "12043/40000"

    conversions = {item["signal"]: item for item in body["conversions"]}
    assert conversions["0.300"]["concentration_interval"] == {
        "lower": "11/20",
        "upper": "11/20",
    }
    assert conversions["1.050"]["concentration_interval"] == {
        "lower": "56/15",
        "upper": "56/15",
    }
    # 拟合信号范围为 [13/80, 11/10]，两侧越界均不外推。
    assert conversions["0.100"]["status"] == "out_of_range"
    assert conversions["0.100"]["concentration_interval"] is None
    assert conversions["1.200"]["status"] == "out_of_range"


def test_docs_examples_are_callable() -> None:
    openapi = client.get("/openapi.json").json()
    examples = openapi["paths"][URL]["post"]["requestBody"]["content"]["application/json"][
        "examples"
    ]
    assert set(examples) == {"slope_limited", "plateau"}
    for example in examples.values():
        response = _post(example["value"])
        assert response.status_code == 200
        assert response.json()["fitted_nodes"]


def test_validation_error_is_located() -> None:
    response = _post(
        {
            "standards": [
                {"concentration": 0, "signal": "0.1", "weight": 1},
                {"concentration": 0, "signal": "oops", "weight": 1},
            ],
            "slope_limit": "0",
            "unknown_signals": ["0.2", 5],
        }
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "curve_validation_error"
    locs = [tuple(issue["loc"]) for issue in detail["errors"]]
    assert ("standards", 1, "concentration") in locs  # 浓度互异
    assert ("standards", 1, "signal") in locs  # 十进制字符串
    assert ("slope_limit",) in locs  # 必须为正
    assert ("unknown_signals", 1) in locs  # 待测信号必须是字符串


def test_missing_fields_rejected_with_locations() -> None:
    response = _post({"standards": []})
    assert response.status_code == 422
    locs = {tuple(issue["loc"]) for issue in response.json()["detail"]["errors"]}
    assert ("standards",) in locs
    assert ("slope_limit",) in locs
    assert ("unknown_signals",) in locs


def test_non_object_body_rejected() -> None:
    response = client.post(URL, json=[1, 2, 3])
    assert response.status_code == 422


def test_demo_json_files_served() -> None:
    for name in ("curve_slope_limited.json", "curve_plateau.json"):
        response = client.get(f"/demo/{name}")
        assert response.status_code == 200
        payload = response.json()
        assert _post(payload).status_code == 200


def test_openapi_documents_curve_endpoint() -> None:
    assert client.get("/docs").status_code == 200
    openapi = client.get("/openapi.json").json()
    post = openapi["paths"][URL]["post"]
    assert "换算" in post["tags"]
    assert "422" in post["responses"]
    schema_ref = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert "StandardCurveResponse" in schema_ref
