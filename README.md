# 访谈字幕时间轴校准 API

基于 Python + FastAPI 的字幕时间轴校准服务。客户端提交以**原录音毫秒**计时的字幕片段，以及至少两组「原时间 → 成片时间」同步锚点；服务端在相邻锚点之间做**分段线性换算**，输出成片时间轴上的字幕起止时间。

同步锚点可手工提供给 `POST /calibrate`，也可由 `POST /anchors/suggest` 根据原片/成片两条字幕序列的文字匹配自动生成；后者返回的锚点可原样传给 `/calibrate`。

## 功能说明

- 接收至少 **2 组**同步锚点 `(original, final)`，服务端校验：
  - `original` 列必须**严格递增**；
  - `final` 列必须**严格递增**。
- 每条字幕片段的 `start`、`end` 各自落在锚点覆盖的区间内，按所在线段独立线性换算：

  ```
  final(t) = final_i + (final_{i+1} - final_i) / (original_{i+1} - original_i) * (t - original_i)
  ```

  跨越一个或多个锚点的片段，其两个端点分别按各自所在线段换算。
- 换算结果**四舍五入到整数毫秒**（0.5 进位），并按输入顺序返回原字幕文字。
- 以下情况返回 `422` 错误，错误体中包含出错片段/锚点的**位置下标**：
  - 锚点少于 2 组；
  - 任一列锚点非严格递增；
  - 片段端点超出锚点覆盖范围（早于首个锚点或晚于末个锚点）；
  - 原片段时长非正（`end <= start`）；
  - 换算后/四舍五入后片段时长非正。
- `POST /anchors/suggest` 接收原片与成片两条字幕（仅 `start` 与 `text`，两轨 `start` 均严格递增），对文字做规范化（去首尾空白、连续空白合并为单个空格）后**精确匹配**，在允许跳过任意片段的前提下求**全局最优**的一对一时间序一致匹配，依次：
  1. 最大化匹配对数；
  2. 最小化配对序号差绝对值之和；
  3. 以上均相同时，按配对序号序列（`[(original_index, final_index), ...]`）字典序裁决。

  重复台词、删段均按全局动态规划处理，不做逐条贪心；匹配少于 2 对时返回 `422`。
- 空文字（去空白后为空）、负 `start`、`start` 非严格递增均返回 `422`，错误项带轨道名（`original` / `final`）与下标。

## 单条启动命令

无需手动安装依赖，依赖安装与服务启动均已纳入容器启动流程（构建镜像时安装依赖，容器启动时启动服务）：

```bash
docker compose up --build -d
```

停止服务：

```bash
docker compose down
```

> 也可以不使用 Compose：
>
> ```bash
> docker build -t subtitle-calibrator .
> docker run -d -p 8000:8000 subtitle-calibrator
> ```

## 必要配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 端口 | 容器内服务端口由环境变量 `PORT` 控制（见 `docker-compose.yml`）；宿主机映射端口为 `${PORT:-8000}`，可在启动前 `export PORT=9000` 修改 | `8000` |

服务无其他外部依赖（无需数据库）。

## API 地址和端口

服务启动后监听 `http://localhost:8000`：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | `GET http://localhost:8000/health` |
| 校准接口 | `POST http://localhost:8000/calibrate` |
| 锚点建议接口 | `POST http://localhost:8000/anchors/suggest` |
| 交互式文档（Swagger UI） | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

## 接口约定

### 请求 `POST /calibrate`

```json
{
  "anchors": [
    { "original": 0, "final": 1000 },
    { "original": 2000, "final": 4000 }
  ],
  "segments": [
    { "start": 0, "end": 500, "text": "大家好" },
    { "start": 500, "end": 2000, "text": "欢迎来到访谈节目" }
  ]
}
```

- `anchors[].original` / `anchors[].final`：锚点的原时间与成片时间，整数毫秒，两列均须严格递增。
- `segments[].start` / `segments[].end`：片段在原录音上的起止时间，整数毫秒，须落在锚点覆盖范围内。
- `segments[].text`：字幕文字，原样返回。

### 成功响应 `200`

```json
{
  "segments": [
    { "index": 0, "start": 1000, "end": 1500, "text": "大家好" },
    { "index": 1, "start": 1500, "end": 4000, "text": "欢迎来到访谈节目" }
  ]
}
```

### 错误响应 `422`

所有片段错误会一次性收集返回，`index` 为片段在输入中的位置（从 0 开始）：

```json
{
  "error": "calibration_failed",
  "detail": [
    {
      "location": "segment",
      "index": 1,
      "code": "out_of_anchor_range",
      "message": "第 1 段 [2100, 2600] 超出锚点覆盖范围 [0, 2000]"
    }
  ]
}
```

错误代码：

| code | 含义 |
| --- | --- |
| `not_enough_anchors` | 锚点少于 2 组 |
| `not_strictly_increasing` | 锚点 original/final 列非严格递增 |
| `out_of_anchor_range` | 片段端点超出锚点覆盖范围 |
| `non_positive_duration` | 原片段 `end <= start` |
| `non_positive_mapped_duration` | 线性换算后时长非正 |
| `non_positive_mapped_duration_after_rounding` | 四舍五入到整数毫秒后时长非正（成片轴压缩过度） |

### 请求 `POST /anchors/suggest`

两条字幕序列均按 `start` 严格递增；`text` 会先做规范化（`strip` 首尾空白，连续空白合并为单个空格），规范化后完全相同才视为可配对：

```json
{
  "original": [
    { "start": 0, "text": "大家好" },
    { "start": 1200, "text": "欢迎来到访谈节目" },
    { "start": 3000, "text": "今天我们聊聊字幕" },
    { "start": 5000, "text": "感谢观看" }
  ],
  "final": [
    { "start": 0, "text": "片头音乐" },
    { "start": 2000, "text": "大家好" },
    { "start": 3500, "text": "欢迎来到   访谈节目" },
    { "start": 6100, "text": "今天我们聊聊字幕" },
    { "start": 8000, "text": "下期再见" }
  ]
}
```

- `original[]` / `final[]`：原片 / 成片字幕轨道。
- `start`：整数毫秒，同一轨道内必须**非负且严格递增**。
- `text`：字幕文字，去空白后不能为空。

### 成功响应 `200`

返回的每个锚点含两个 `start`（字段名与 `/calibrate` 入参一致，可直接透传）以及来源下标：

```json
{
  "anchors": [
    { "original": 0,    "final": 2000, "original_index": 0, "final_index": 1 },
    { "original": 1200, "final": 3500, "original_index": 1, "final_index": 2 },
    { "original": 3000, "final": 6100, "original_index": 2, "final_index": 3 }
  ]
}
```

### 匹配规则

- 允许跳过任一轨道上的任意片段（对应片头/片中删段、重复台词的取舍）；
- 配对必须一一对应且保持时间顺序（两列下标均严格递增）；
- 全局最优，按顺序采用三条裁决标准：① 匹配对数最多；② 各配对 `|original_index - final_index|` 之和最小；③ 配对序号序列字典序最小。因此重复台词不会被逐条贪心错配。

### 错误响应 `422`

匹配少于 2 对：

```json
{
  "error": "anchor_suggestion_failed",
  "detail": [
    {
      "location": "anchors",
      "index": null,
      "code": "not_enough_matches",
      "message": "可用匹配仅 1 对，至少需要 2 对才能生成同步锚点"
    }
  ]
}
```

轨道内容错误会一次性收集，`location` 标明轨道与字段，`index` 为轨道内下标：

```json
{
  "error": "anchor_suggestion_failed",
  "detail": [
    { "location": "original.start", "index": 1, "code": "not_strictly_increasing", "message": "……" },
    { "location": "final.start",    "index": 0, "code": "negative_time",         "message": "……" },
    { "location": "final.text",     "index": 2, "code": "empty_text",            "message": "……" }
  ]
}
```

| code | 含义 |
| --- | --- |
| `not_enough_matches` | 文字可配对数少于 2 对 |
| `negative_time` | 某条字幕 `start < 0` |
| `not_strictly_increasing` | 轨道内 `start` 非严格递增 |
| `empty_text` | 文字去空白后为空 |

请求体结构/类型错误（缺少字段、非整数等）仍返回统一的 `invalid_request`。

## 调用示例

```bash
curl -s -X POST http://localhost:8000/calibrate \
  -H "Content-Type: application/json" \
  -d '{
    "anchors": [
      {"original": 0, "final": 0},
      {"original": 3000, "final": 1000}
    ],
    "segments": [
      {"start": 900, "end": 2100, "text": "这是一段访谈字幕"}
    ]
  }'
```

响应：

```json
{
  "segments": [
    { "index": 0, "start": 300, "end": 700, "text": "这是一段访谈字幕" }
  ]
}
```

### 自动生成锚点：`POST /anchors/suggest`

```bash
curl -s -X POST http://localhost:8000/anchors/suggest \
  -H "Content-Type: application/json" \
  -d '{
    "original": [
      {"start": 0, "text": "大家好"},
      {"start": 1200, "text": "欢迎来到访谈节目"}
    ],
    "final": [
      {"start": 500, "text": "大家好"},
      {"start": 2900, "text": "欢迎来到   访谈节目"}
    ]
  }'
```

响应中的 `anchors` 字段名与 `/calibrate` 入参一致，可直接透传（如需可先增删锚点）：

```json
{
  "anchors": [
    {"original": 0, "final": 500, "original_index": 0, "final_index": 0},
    {"original": 1200, "final": 2900, "original_index": 1, "final_index": 1}
  ]
}
```

一条命令串联「建议锚点 → 校准」（需要 `jq`）：

```bash
curl -s -X POST http://localhost:8000/anchors/suggest \
  -H "Content-Type: application/json" \
  -d '{"original":[{"start":0,"text":"大家好"},{"start":1200,"text":"欢迎来到访谈节目"}],"final":[{"start":500,"text":"大家好"},{"start":2900,"text":"欢迎来到访谈节目"}]}' \
  | jq '{anchors: .anchors, segments: [{"start": 600, "end": 1200, "text": "待校准片段"}]}' \
  | curl -s -X POST http://localhost:8000/calibrate -H "Content-Type: application/json" -d @-
```

## 本地开发（不用容器）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   └── main.py          # FastAPI 应用、校验与线性换算逻辑
├── Dockerfile           # 构建期安装依赖，容器启动即启动服务
├── docker-compose.yml   # 一键编排
├── requirements.txt
└── README.md
```
