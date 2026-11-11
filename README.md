# 水质实验室检测结果归属核对 API

用 FastAPI 接收检测任务 CSV 与仪器离线结果 CSV，求“先最多配对、再最小计划时间偏差总和”的全部最优方案，并返回：

- `definite_pairs`：所有最优方案**共有**的确定配对，可安全导入实验室系统；
- `ambiguous_tasks` / `ambiguous_results`：其余记录在某个最优方案中可能对应的候选，以及该记录是否可能在某个最优方案中未配对。

服务不在有歧义时擅自挑选一个“看起来最像”的配对，从而避免误归属。

另提供**标准曲线换算**接口：接收仪器导出的 2~12 个标准点（浓度、信号、正整数权重）、正斜率上限与最多 30 个待测信号，在“拟合节点不降、相邻增量 ≤ 斜率上限 × 浓度差”约束下使加权信号残差平方和最小，用有理数精确求解并以最简分数返回拟合节点；随后按节点间线性插值在标准浓度范围内反算每个待测信号的完整浓度闭区间，平台段整段保留，超出拟合信号范围标为无法换算，不外推。这样标准点噪声不会造成浓度倒挂。

## 快速启动

```bash
docker compose up --build
```

启动后：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI，可上传 CSV 或运行内置标准曲线示例 |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/openapi.json> | OpenAPI 文档 |
| <http://localhost:8000/demo/tasks.csv> | 含歧义的演示任务 CSV |
| <http://localhost:8000/demo/results.csv> | 含歧义的演示结果 CSV |
| <http://localhost:8000/demo/curve_slope_limited.json> | 标准曲线演示请求（斜率受限） |
| <http://localhost:8000/demo/curve_plateau.json> | 标准曲线演示请求（平台段） |
| <http://localhost:8000/health> | 容器健康检查 |

如 8000 端口被占用：`APP_PORT=8080 docker compose up --build`。停止服务用 `docker compose down`。

也可以直接用 curl：

```bash
curl -X POST http://localhost:8000/v1/reconciliations \
  -F "tasks=@demo/tasks.csv;type=text/csv" \
  -F "results=@demo/results.csv;type=text/csv"

curl -X POST http://localhost:8000/v1/standard-curve-conversions \
  -H "Content-Type: application/json" \
  --data-binary @demo/curve_plateau.json
```

在 Swagger UI 中打开 `POST /v1/reconciliations`，点击 **Try it out**，两个文件字段分别选择上面下载的演示 CSV，再点击 **Execute**。标准曲线接口打开 `POST /v1/standard-curve-conversions` 后在 **Examples** 下拉框中选择“斜率受限示例”或“平台段示例”即可直接运行。

## 接口

### `POST /v1/reconciliations`

`multipart/form-data`，两个文件字段均为必填：

| 字段 | 内容 |
| --- | --- |
| `tasks` | 检测任务 CSV，最多 30 行数据（不含表头） |
| `results` | 仪器离线结果 CSV，最多 30 行数据（不含表头） |

成功时返回 `200` 和如下结构：

```json
{
  "summary": {
    "task_records": 6,
    "result_records": 7,
    "eligible_candidate_pairs": 7,
    "max_pairs": 4,
    "minimum_total_absolute_deviation_seconds": 2700,
    "definite_pair_count": 2,
    "ambiguous_task_count": 4,
    "ambiguous_result_count": 5
  },
  "definite_pairs": [
    {
      "task_id": "T-004",
      "result_id": "R-004",
      "sample_code": "S-300",
      "item": "Pb",
      "planned_time": "2026-11-11T14:00:00Z",
      "allowed_start": "2026-11-11T13:00:00Z",
      "allowed_end": "2026-11-11T15:00:00Z",
      "test_time": "2026-11-11T14:10:00Z",
      "value": "0.012",
      "deviation_seconds": 600
    }
  ],
  "ambiguous_tasks": [
    {
      "task_id": "T-001",
      "sample_code": "S-100",
      "item": "pH",
      "planned_time": "2026-11-11T10:00:00Z",
      "allowed_start": "2026-11-11T09:00:00Z",
      "allowed_end": "2026-11-11T11:00:00Z",
      "possible_result_pairs": [
        {
          "result_id": "R-001",
          "test_time": "2026-11-11T09:30:00Z",
          "value": "7.10",
          "deviation_seconds": 1800
        },
        {
          "result_id": "R-002",
          "test_time": "2026-11-11T10:30:00Z",
          "value": "7.20",
          "deviation_seconds": 1800
        }
      ],
      "possibly_unpaired": false
    }
  ],
  "ambiguous_results": [
    {
      "result_id": "R-005",
      "sample_code": "S-300",
      "item": "Pb",
      "test_time": "2026-11-11T14:30:00Z",
      "value": "0.011",
      "possible_task_pairs": [],
      "possibly_unpaired": true
    }
  ]
}
```

（示例只截取了部分数组元素；实际响应会逐条列出所有未确定记录。）

#### 响应语义

- `definite_pairs`：在所有最优方案中都出现的边。每条任务和结果最多出现一次。**只有这些可以自动导入。**
- `ambiguous_tasks[*].possible_result_pairs`：该任务在至少一个最优方案中可能配对的结果全集。
- `ambiguous_results[*].possible_task_pairs`：该结果在至少一个最优方案中可能配对的任务全集。
- `possibly_unpaired = true`：存在至少一个最优方案令该记录不配对。即使候选列表只有一条，也不能据此自动归属。
- `possible_*_pairs = []` 且 `possibly_unpaired = true`：该记录在所有最优方案中都只能不配对。
- 已进入 `definite_pairs` 的任务/结果不会再出现在歧义数组中。

### 错误

CSV 格式、表头、字段值、行数或时间格式不合规时返回 `422`：

```json
{
  "detail": {
    "code": "csv_validation_error",
    "message": "tasks CSV 表头不完整。",
    "errors": [
      {
        "file": "tasks",
        "row": 1,
        "field": "allowed_start/allowed_end",
        "message": "缺少允许检测时段：请提供 allowed_start 与 allowed_end 两列，或 allowed_interval 一列。"
      }
    ]
  }
}
```

`row` 是 CSV 物理行号，表头为第 1 行。两个文件都会校验，便于一次修正全部问题。

## 标准曲线换算

### `POST /v1/standard-curve-conversions`

`application/json`。用于把仪器信号换算成浓度：先对标准点做**斜率受限的加权最小二乘拟合**，
再对待测信号做**不外推的反算**。

请求体：

```json
{
  "standards": [
    {"concentration": 0, "signal": "1.00", "weight": 1},
    {"concentration": 1, "signal": "0.90", "weight": 1},
    {"concentration": 2, "signal": "0.95", "weight": 2},
    {"concentration": 3, "signal": "2.00", "weight": 1},
    {"concentration": 4, "signal": "2.10", "weight": 1}
  ],
  "slope_limit": "5",
  "unknown_signals": ["0.95", "1.50", "0.50"]
}
```

| 字段 | 规则 |
| --- | --- |
| `standards` | 2~12 个标准点 |
| `standards[].concentration` | 非负整数（≤ 999999999），互异；JSON 数字，不接受字符串/布尔/小数 |
| `standards[].signal` | 十进制字符串，如 `"0.125"`、`"1e-3"`；拒绝 NaN/Infinity |
| `standards[].weight` | 正整数（≤ 1000000） |
| `slope_limit` | 正斜率上限，十进制字符串 |
| `unknown_signals` | 最多 30 个待测信号，均为十进制字符串 |

**拟合规则**：按浓度升序排列后，拟合节点 `f` 须不降，且相邻增量
`f(i+1) − f(i) ≤ slope_limit × 浓度差`；在此约束下使 `Σ w_i·(f_i − y_i)²` 最小。
全程用 `Fraction` 有理数精确求解（原始积极集法解凸二次规划，解唯一），
拟合节点与最小残差平方和都以**最简分数**返回（分母为 1 时只给整数，如 `"2"`）。

**反算规则**：节点间线性插值，仅在标准浓度范围内反算。每个待测信号返回完整浓度
闭区间 `[lower, upper]`——曲线连续且不降，原像必为闭区间；平台段（相邻拟合节点
相等）整段保留。信号超出拟合信号范围 `[f_1, f_n]` 时 `status = "out_of_range"`，
不给出区间、不外推。

成功响应（200）节选：

```json
{
  "summary": {
    "standard_count": 5,
    "unknown_count": 3,
    "slope_limit": "5",
    "min_concentration": 0,
    "max_concentration": 4,
    "min_fitted_signal": "19/20",
    "max_fitted_signal": "21/10",
    "weighted_residual_sum_squares": "1/200"
  },
  "fitted_nodes": [
    {"concentration": 0, "observed_signal": "1.00", "weight": 1, "fitted_signal": "19/20"}
  ],
  "conversions": [
    {"signal": "0.95", "status": "ok", "concentration_interval": {"lower": "0", "upper": "2"}},
    {"signal": "1.50", "status": "ok", "concentration_interval": {"lower": "53/21", "upper": "53/21"}},
    {"signal": "0.50", "status": "out_of_range", "concentration_interval": null}
  ]
}
```

上例即 `/docs` 中的“平台段示例”（演示文件
[`demo/curve_plateau.json`](demo/curve_plateau.json)）：前三个标准点因噪声出现倒挂，
被加权池化为平台 `19/20`，覆盖浓度 0~2 整段；信号 `0.95` 命中平台，返回整段闭区间
`[0, 2]`。“斜率受限示例”（[`demo/curve_slope_limited.json`](demo/curve_slope_limited.json)）
则演示中段信号被斜率上限 `0.25` 截断，以及两个方向越界信号的 `out_of_range` 标记。

#### 换算接口错误

非法输入返回 `422`，`errors` 一次性列出全部问题并用 JSON 路径定位：

```json
{
  "detail": {
    "code": "curve_validation_error",
    "message": "请求存在 2 处非法输入。",
    "errors": [
      {"loc": ["standards", 1, "concentration"], "message": "浓度 0 与第 0 个标准点重复；浓度必须互异。"},
      {"loc": ["slope_limit"], "message": "斜率上限必须是正数，实际为 '0'。"}
    ]
  }
}
```

## CSV 格式

UTF-8（可带 BOM），逗号分隔；也兼容分号、制表符分隔。空白数据行会被跳过。

### 检测任务 `tasks`

| 列名 | 必填 | 说明 |
| --- | --- | --- |
| `task_id` | 是 | 任务唯一编号，文件内不可重复 |
| `sample_code` | 是 | 样品码 |
| `item` | 是 | 检测项目 |
| `planned_time` | 是 | 计划检测时间 |
| `allowed_start` | 是* | 允许检测时段开始（含） |
| `allowed_end` | 是* | 允许检测时段结束（含） |
| `allowed_interval` | 是* | 可用 `START/END` 代替上面两列，如 `2026-11-11T09:00:00Z/2026-11-11T11:00:00Z` |

\* `allowed_start` + `allowed_end` 必须同时提供，**或**提供 `allowed_interval`。若同时提供，优先使用分列；分列缺失时尝试 `allowed_interval`。

任务示例：

```csv
task_id,sample_code,item,planned_time,allowed_start,allowed_end
T-001,S-100,pH,2026-11-11T10:00:00Z,2026-11-11T09:00:00Z,2026-11-11T11:00:00Z
```

### 仪器离线结果 `results`

| 列名 | 必填 | 说明 |
| --- | --- | --- |
| `result_id` | 是 | 结果唯一编号，文件内不可重复 |
| `sample_code` | 是 | 样品码，必须与任务完全一致才可能配对 |
| `item` | 是 | 检测项目，必须与任务完全一致才可能配对 |
| `test_time` | 是 | 实际检测时间 |
| `value` | 是 | 数值，必须为有限数；响应中保留原始文本 |

结果示例：

```csv
result_id,sample_code,item,test_time,value
R-001,S-100,pH,2026-11-11T09:30:00Z,7.10
```

### 时间与文本规则

- 时间必须显式标注 UTC：推荐 `YYYY-MM-DDTHH:MM:SSZ`，也接受 `+00:00`。
- 必须是整秒；拒绝小数秒和非 UTC 偏移（如 `+08:00`）。
- 响应中的时间统一规范化为 `YYYY-MM-DDTHH:MM:SSZ`。
- 样品码与检测项目区分大小写，按去除首尾空白后的完整字符串精确比较。
- 以下表头别名也可使用（大小写不敏感）：
  - 任务：`编号/唯一编号/任务编号`、`样品码/样品编号`、`检测项目/项目`、`计划时间`、`允许检测时段`等；
  - 结果：`结果编号/唯一编号`、`样品码/样品编号`、`检测项目/项目`、`检测时间`、`数值/结果值`等。
  推荐仍使用上面的规范英文列名。

## 优化与判定算法

1. 候选边：样品码、项目精确相同，且 `allowed_start <= test_time <= allowed_end`（闭区间）。
2. 建立源点 → 任务 → 结果 → 汇点的单位容量网络；任务-结果边费用为
   `|test_time - planned_time|` 秒。
3. 先求最大流（最大配对数），再在最大流下求最小费用（最小偏差总和）。
4. 在求得的最优流上计算残量网络和合法节点势能，只保留**缩减费用为 0** 的残量弧。任意另一个同值同费用的最优解，都对应这些零费用弧构成的环流。
5. 对零费用残量图求强连通分量（SCC）：
   - 未使用的任务-结果边若在某个零费用环中（两端同 SCC），则它出现在某个最优解中；
   - 已使用边的反向弧若在零费用环中，则该边可被其他最优解替换，不属于确定配对；
   - 任务/结果与源点/汇点的反向弧是否在零费用环中，决定它是否可能未配对。
6. 确定配对 = 当前最优解中使用、且反向弧不在任何零费用环中的边。确定配对集合不依赖每一条最优解的枚举，因此即使最优解数量很大也能快速给出精确交集和候选并集。

规模上限为每侧 30 条，候选边最多 900 条；算法运行时间与网络规模多项式相关，不随最优解数量指数增长。

## 演示数据说明

`demo/tasks.csv` 与 `demo/results.csv` 有意覆盖：

| 情形 | 记录 | 预期 |
| --- | --- | --- |
| 对称二选一，偏差同为 1800 秒 | `T-001` / `R-001`、`R-002` | 无确定配对；任务总会被配对，两个结果均可能未配对 |
| 两个任务竞争同一结果 | `T-002`、`T-003` / `R-003` | 无确定配对；结果总会被配对，两个任务均可能未配对 |
| 偏差不同，唯一最优 | `T-004` / `R-004`（600 秒）与 `R-005`（1800 秒） | `T-004-R-004` 确定；`R-005` 必然未配对 |
| 无竞争，唯一最优 | `T-005` / `R-006` | `T-005-R-006` 确定 |
| 无候选 | `T-006`、`R-007` | 必然未配对 |

该演示的最优目标为：`max_pairs = 4`，`minimum_total_absolute_deviation_seconds = 2700`，确定配对 2 条。

## 本地开发与测试

需要 Python 3.11+：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

测试包括：

- CSV 别名、合并时段、BOM、分号分隔、严格 UTC 校验、30 行上限、重复编号与数值校验；
- 随机小规模图与穷举所有匹配的交叉验证（确定配对、候选并集、可能未配对均逐项比对）；
- 标准曲线：十进制字符串严格校验与逐字段定位、积极集法与穷举积极集暴力解的随机交叉验证
  （Fraction 精确相等）、平台段整段保留、精确分数插值、越界不外推；
- 演示数据预期、API 成功/422 响应、OpenAPI 与演示文件可用性、/docs 内置示例可运行。

## 文件结构

```
app/
  main.py          FastAPI 路由、OpenAPI 描述、响应组装
  csv_parser.py    严格 CSV 解析与中文/英文表头别名
  curve.py         标准曲线：输入校验、斜率受限 QP 精确求解、信号反算
  domain.py        任务与结果领域对象、UTC 格式化
  optimizer.py     最大流最小费用 + 零费用残量图 SCC 分类
  schemas.py       Pydantic 响应/错误模型（OpenAPI 文档来源）
demo/
  tasks.csv        歧义演示任务
  results.csv      歧义演示结果
  curve_slope_limited.json   标准曲线演示请求（斜率受限）
  curve_plateau.json         标准曲线演示请求（平台段）
tests/
Dockerfile
docker-compose.yml
requirements.txt
```

## 容器运维

```bash
docker compose ps
docker compose logs -f api
docker compose down
```

Compose 使用非 root 用户运行容器，并配置了 `/health` 健康检查；服务重启策略为 `unless-stopped`。
