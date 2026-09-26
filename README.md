# 访谈字幕时间轴校准 API

基于 Python + FastAPI 的字幕时间轴校准服务。客户端提交以**原录音毫秒**计时的字幕片段，以及至少两组「原时间 → 成片时间」同步锚点；服务端在相邻锚点之间做**分段线性换算**，输出成片时间轴上的字幕起止时间。

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
