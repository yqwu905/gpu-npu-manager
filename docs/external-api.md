# 本地外部应用 API

适用于本机脚本、桌面程序和浏览器应用，覆盖服务器查询、任务查询、任务创建、推理结果登记、运行评测工程和查看评测结果。接口与管理页面共用数据库、调度器和评测流程。

## 启动与约定

在项目根目录安装依赖后启动中心服务：

```sh
pip install -r server/requirements.txt
cd server
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

- 基础地址：`http://127.0.0.1:8000`；接口前缀：`/api`。
- 无需鉴权，不传 `Authorization`、API Key 或 Cookie。现有节点配置了令牌时，启动中心服务前按原配置设置 `GNM_AGENT_TOKEN`；它只用于中心服务与 Agent 通信。
- 请求体使用 JSON，带 `Content-Type: application/json`。时间为 UTC ISO 8601，取不到的数据为 `null`。
- 允许 `http` / `https` 的 `localhost`、`127.0.0.1`、`[::1]` 来源从任意端口跨域调用，无需浏览器凭证；本地网页请通过 HTTP 服务打开。
- Swagger：`http://127.0.0.1:8000/docs`；ReDoc：`http://127.0.0.1:8000/redoc`；OpenAPI：`http://127.0.0.1:8000/openapi.json`。
- 离线接口定义见 [openapi.json](openapi.json)，所有接口细节见 [api.md](api.md)。在线文档页面使用 CDN 脚本，完全离线时可将 OpenAPI 文件导入本地 API 工具。
- 服务器需已登记、Agent 可连通。任务与评测异步执行，后台轮询和调度需保持启用（默认启用）。

| 能力 | 方法与路径 |
| --- | --- |
| 查询服务器列表与空闲状态 | `GET /api/servers`、`GET /api/servers/{server_id}` |
| 查询任务 | `GET /api/jobs`、`GET /api/jobs/{job_id}` |
| 创建任务 | `POST /api/jobs` |
| 添加推理结果 | `POST /api/results` |
| 运行评测 | `POST /api/evaluations`（单个结果集）、`POST /api/projects/{project_id}/evaluations`（项目批量） |
| 查看评测结果 | `GET /api/evaluations`、`GET /api/evaluations/{evaluation_id}` |

以下 curl 示例中的 ID 和服务器路径需替换成实际值。创建类请求返回 `201`，表示记录已创建；查询成功返回 `200`。

## 1. 查询服务器与空闲状态

```sh
curl 'http://127.0.0.1:8000/api/servers?status=online&schedulable=true&has_idle=true'
curl 'http://127.0.0.1:8000/api/servers/1'
# 需要最新采样时刷新指定服务器
curl -X POST 'http://127.0.0.1:8000/api/servers/1/refresh'
```

列表返回数组，以下仅列出主要字段：

```json
[
  {
    "id": 1, "name": "gpu-01", "status": "online", "schedulable": true,
    "accelerator": "gpu", "device_count": 2, "idle_device_count": 1,
    "last_seen_at": "2026-11-05T02:00:00Z", "last_error": null,
    "devices": [
      {"index": 0, "idle": false, "job_id": 42, "memory_used_mb": 12000},
      {"index": 1, "idle": true, "job_id": null, "memory_used_mb": 0}
    ]
  }
]
```

可选筛选：`q`、`group`、`owner`、`tag`、`accelerator=gpu|npu`、`model`、`status=online|offline|unknown`、`schedulable`、`has_idle`。`group`、`owner`、`model` 可重复，满足其一；`tag` 可重复，必须全部包含，如 `?tag=lab&tag=a100`。`has_idle=false` 查询没有空闲卡的服务器。`include_devices=false` 省略逐卡详情，汇总计数仍保留。

一张卡被判定为空闲，需要服务器在线、健康状态可用（OK / Warning 或未提供）、没有进程、已用显存低于阈值（GPU 默认 1024 MB，NPU 默认 6144 MB），并且没有被 `starting` / `running` / `lost` 的平台任务占用。`schedulable` 另行表示是否允许调度，查询可调度资源时应同时指定它。

状态来自后台采样，默认每 10 秒更新，连续 3 次失败判离线；结合 `last_seen_at` 与 `last_error` 判断新鲜度。查询不会预留资源，实际分配以调度器为准。

## 2. 查询任务

```sh
curl 'http://127.0.0.1:8000/api/jobs?status=queued&status=running&limit=50&offset=0'
curl 'http://127.0.0.1:8000/api/jobs/42'
curl 'http://127.0.0.1:8000/api/jobs/42/log?offset=0&limit=65536'
```

列表返回 `{"total": 120, "items": [...]}`，详情直接返回任务对象。筛选参数：

| 参数 | 说明 |
| --- | --- |
| `status` | 可重复；`queued`、`starting`、`running`、`lost`、`succeeded`、`failed`、`cancelled` |
| `group`、`submitter` | 运行组、提交人精确匹配 |
| `server_id` | 实际分配到的服务器，排队中尚未分配的任务不会匹配 |
| `q` | 搜索名称、命令、提交人；数字或 `#数字` 同时匹配任务 ID |
| `limit`、`offset` | 默认 50、0；limit 为 1–500，offset 非负 |

默认按 ID 倒序；只筛选 `queued` 时按调度顺序。主要字段有 `id`、`status`、`queue_position`、`wait_reason`、`assigned_server_id`、`device_indices`、`exit_code`、`error`。日志接口返回 `data`、`size`、`next_offset` 等，后续请求使用上次的 `next_offset`。

## 3. 创建任务

```sh
curl -X POST 'http://127.0.0.1:8000/api/jobs' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "model-a inference",
    "command": "python infer.py --output /data/results/model-a",
    "workdir": "/data/model-a",
    "env": {"PYTHONUNBUFFERED": "1"},
    "server_id": 1,
    "num_devices": 1,
    "priority": 0,
    "submitter": "external-app"
  }'
```

| 字段 | 必填 / 默认 | 说明 |
| --- | --- | --- |
| `command` | 必填 | 非空白 shell 命令，在目标服务器以 Agent 用户的 `bash -l` 执行 |
| `name` | 命令开头 | 任务名称，最多 255 字符 |
| `workdir` | 用户家目录 | 目标服务器上的工作目录 |
| `env` | `{}` | 额外环境变量，值为字符串，变量名需合法 |
| `num_devices` | `1` | 0–64，0 表示 CPU 任务 |
| `server_id` | `null` | 指定服务器；不填时自动选择 |
| `group`、`accelerator` | `null` | 调度约束；accelerator 可为 `gpu` 或 `npu` |
| `priority` | `0` | -100 至 100，数值越大优先级越高 |
| `submitter` | `null` | 提交人标记，最多 128 字符 |
| `preemptible` | `false` | 是否允许高优先级任务抢占，被抢占后从头重新运行 |

返回任务对象，例如主要字段为 `{"id":42,"status":"queued","queue_position":1,"assigned_server_id":null,"device_indices":[]}`。提交时要求至少有一台可调度服务器满足约束与总卡数；暂时没有空闲卡时会排队。

默认调度间隔 5 秒。轮询任务详情直至 `succeeded` / `failed` / `cancelled`；`lost` 表示 Agent 失联，仍可能恢复。失败时查看 `error`、`exit_code` 和日志。生成结果后再调用结果登记接口，任务不会自动登记输出目录。

## 4. 添加推理结果

```sh
curl -X POST 'http://127.0.0.1:8000/api/results' \
  -H 'Content-Type: application/json' \
  -d '{
    "server_id": 1,
    "path": "/data/results/model-a",
    "name": "model-a",
    "job_id": 42,
    "project_id": 2,
    "tags": ["baseline"],
    "note": "external-app output"
  }'
```

`server_id`、`path` 必填；其他字段可省略。`path` 是结果所在服务器上 Agent 可读的绝对目录。接口登记已有目录，并读取样本和可选的 `meta.json`；文件需先由推理任务或应用写入该目录。`job_id` 用于关联产生结果的任务；`project_id` 用于归档到已存在的项目，不填则未归档。

支持两种结果格式：

- 目录内有 `predictions.jsonl`：每行一个带 `id` 的 JSON 样本，可含 `image` / `ref_image`、`text` / `ref_text`，图片相对路径以 jsonl 所在目录为基准。
- 没有 jsonl：扫描图片和 `.txt`，去掉扩展名的相对路径作为样本 ID，同名图片与文本合成一个样本；跳过 `eval/` 和隐藏文件。

例如文字样本可以是：

```json
{"id":"sample-1","text":"识别结果","ref_text":"真实文本"}
```

返回结果集对象，保存其中的 `id`（如 7），后续作为 `result_set_id`。主要字段为 `id`、`name`、`server_id`、`path`、`sample_count`、`project_id`、`metrics`、`evaluating`。未评测时 `metrics` 为 `{}`。

查询登记记录：`GET /api/results?project_id=2` 或 `GET /api/results/7`。不存在、不可读或没有样本的目录返回 422，Agent 连接失败返回 502。

## 5. 运行评测工程

平台的“项目”负责归档结果集，“评测配置”保存指标与数据路径。可对一个结果集运行配置，也可对项目中的结果集批量运行。

### 准备项目与评测配置

查询现有对象：`GET /api/projects`、`GET /api/eval-configs`、`GET /api/evaluators`。按需创建：

```sh
curl -X POST 'http://127.0.0.1:8000/api/projects' \
  -H 'Content-Type: application/json' -d '{"name":"SR benchmark"}'
curl -X POST 'http://127.0.0.1:8000/api/eval-configs' \
  -H 'Content-Type: application/json' \
  -d '{"name":"SR default","metrics":["psnr","ssim"],"gt_dir":"/data/GT","num_devices":0}'
```

将结果集归档到项目，可在登记时给 `project_id`，或调用 `PATCH /api/results/7`，请求体 `{"project_id":2}`。

可用指标：`psnr`、`ssim`、`lpips`、`ocr_a`、`cer`、`ned`（1-NED）。评测脚本需 numpy、Pillow；LPIPS 需要 torch、lpips；样本没有现成识别文本时，文字指标还需要 PaddleOCR 和 PaddlePaddle。具体环境准备见项目 README。

### 单个结果集评测

```sh
curl -X POST 'http://127.0.0.1:8000/api/evaluations' \
  -H 'Content-Type: application/json' \
  -d '{"result_set_id":7,"config_id":3,"submitter":"external-app"}'
```

也可以不用配置，直接传 `{"result_set_id":7,"metrics":["psnr","ssim"],"gt_dir":"/data/GT","num_devices":0}`。

必填 `result_set_id`，且 `config_id` 或非空 `metrics` 至少有一个。请求中的 `metrics`、`label_file`、`gt_dir`、`lq_dir`、`server_id`、`server_path`、`num_devices`、`python` 覆盖配置；省略的字段沿用配置。其他可选参数为 `reference`、`recompute_lq=false`、`priority=0`、`submitter`。

`num_devices` 默认 0（CPU）；需要加速时可指定卡数。`python` 指定评测服务器上的解释器。参考数据可用 `gt_dir`（图片）、`label_file`（PaddleOCR 标注），或 `reference`（目录、jsonl、标注文件）。有 `lq_dir` 时还计算并复用 LQ 基线。

未指定评测服务器时在结果所在服务器上运行。指定另一台 `server_id` 时需给 `server_path`，且两台服务器均填写 SSH 用户并允许中心服务免密连接；平台先拷贝结果目录，再排队。配置中的路径均在实际评测服务器上。

返回评测对象：保存 `id`（评测 ID）和 `job_id`（任务 ID）。初始 `status` 为 `pending`，跨服务器时为 `copying`，此时 `job_id` 暂为 `null`，拷贝完成后才生成任务。

### 批量运行项目

```sh
# 项目内全部结果集应用配置 3
curl -X POST 'http://127.0.0.1:8000/api/projects/2/evaluations' \
  -H 'Content-Type: application/json' \
  -d '{"config_id":3,"priority":10,"submitter":"external-app"}'

# 仅运行指定子集，也可覆盖配置中的字段
curl -X POST 'http://127.0.0.1:8000/api/projects/2/evaluations' \
  -H 'Content-Type: application/json' \
  -d '{"result_set_ids":[7,8],"config_id":3,"metrics":["psnr"]}'
```

请求使用与单次评测相同的参数，以可选 `result_set_ids` 代替 `result_set_id`。返回结构如下，`items` 中是完整评测对象，此处仅列出主要字段：

```json
{
  "project_id": 2,
  "total": 2,
  "items": [
    {"id": 12, "result_set_id": 7, "status": "pending", "job_id": 88},
    {"id": 13, "result_set_id": 8, "status": "pending", "job_id": 89}
  ]
}
```

- 不传 `result_set_ids` 时选取项目中全部结果集；单次最多 500 个，可用子集分批提交。
- 指定 ID 必须存在且属于该项目，重复 ID 自动去重，响应按结果集 ID 升序。
- 未定义的请求字段返回 422；子集字段使用复数 `result_set_ids`。
- 项目不存在返回 404；空项目、超限或参数错误返回 422。参数校验与所有记录创建在同一事务完成，失败时整批回滚且不启动拷贝。
- 提交成功后每项独立运行，保存本批 `items[].id` 逐个查询。异步拷贝或运行仍可能失败。
- 创建任务、登记结果和发起评测均为新增操作；重复请求会生成新记录，客户端遇到请求超时时应先查询确认。

## 6. 查看评测结果

```sh
curl 'http://127.0.0.1:8000/api/evaluations/12'
curl 'http://127.0.0.1:8000/api/evaluations?project_id=2&config_id=3&status=succeeded&limit=100&offset=0'
curl 'http://127.0.0.1:8000/api/evaluations?result_set_id=7'
curl 'http://127.0.0.1:8000/api/results/7/samples?limit=50&offset=0'
curl 'http://127.0.0.1:8000/api/compare/metrics?ids=7&ids=8'
```

列表返回数组，按评测 ID 倒序；支持 `result_set_id`、`project_id`、`config_id`、重复的 `status`、`limit`（默认 100，1–500）、`offset`（默认 0），翻页直至返回空数组。不同筛选条件取交集，多个状态取并集。`project_id` 根据结果集当前归属筛选；查询某批精确结果应使用提交返回的评测 ID。

评测详情的主要字段：

```json
{
  "id": 12, "result_set_id": 7, "config_id": 3,
  "status": "succeeded", "job_id": 88, "job_status": "succeeded",
  "values": {"psnr": 28.41, "ssim": 0.873},
  "counts": {"psnr": 1000, "ssim": 1000},
  "errors": null, "num_skipped": 0, "error": null,
  "progress": null,
  "output_dir": "/data/results/model-a/eval/12"
}
```

| `status` | 含义 |
| --- | --- |
| `copying` | 正在拷贝到评测服务器 |
| `pending` | 等待评测任务调度 |
| `running` | 正在运行；`progress` 可包含 `stage`、`done`、`total`、`elapsed` |
| `succeeded` | 已收集指标；仍需检查 `errors` 和各指标值 |
| `failed` | 拷贝、执行或收集失败，原因在 `error` |

建议每 2–5 秒轮询详情，设置符合工程规模的超时；运行进度由脚本约每 10 秒更新。`values` 是整体指标，`counts` 是参与计算的样本数；某指标不能计算时值为 `null`，原因在 `errors`。`succeeded` 允许部分指标缺失。LQ 指标在 `lq_values`、`lq_counts`、`lq_errors`。

`GET /api/results/{id}` 的 `metrics` 是每个指标最近一次成功评测的汇总，可能来自不同评测；复现某次运行请用评测详情的 `values`。逐样本浏览接口返回样本及其 `metrics`，多组对比接口按结果集对齐指标。

## Python 最小调用示例

使用中心服务已有的 httpx 依赖。先在界面或 API 中登记服务器，将三个路径改成该服务器上的实际路径。示例创建推理任务、等待完成、登记输出、运行评测并读取指标：

```python
import shlex
import time
import httpx

SERVER_ID = 1
WORKDIR = "/data/model-a"
RESULT_DIR = "/data/results/model-a"
GT_DIR = "/data/GT"

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as api:
    def call(method, path, **kwargs):
        response = api.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def wait(path, terminal, timeout=3600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = call("GET", path)
            if item["status"] in terminal:
                if item["status"] != "succeeded":
                    raise RuntimeError(item.get("error") or item["status"])
                return item
            time.sleep(3)
        raise TimeoutError(path)

    print(call("GET", "/api/servers", params={"status": "online"}))
    print(call("GET", "/api/jobs", params={"limit": 10}))
    job = call("POST", "/api/jobs", json={
        "command": f"python infer.py --output {shlex.quote(RESULT_DIR)}",
        "workdir": WORKDIR, "server_id": SERVER_ID, "num_devices": 1,
        "submitter": "external-app",
    })
    job = wait(f"/api/jobs/{job['id']}", {"succeeded", "failed", "cancelled"})
    result = call("POST", "/api/results", json={
        "server_id": job["assigned_server_id"], "path": RESULT_DIR, "job_id": job["id"],
    })
    evaluation = call("POST", "/api/evaluations", json={
        "result_set_id": result["id"], "metrics": ["psnr", "ssim"], "gt_dir": GT_DIR,
    })
    evaluation = wait(f"/api/evaluations/{evaluation['id']}", {"succeeded", "failed"})
    print("metrics:", evaluation["values"])
    print("metric errors:", evaluation["errors"])
```

浏览器调用同样无需令牌，例如：

```javascript
const response = await fetch("http://127.0.0.1:8000/api/jobs", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ command: "python infer.py", server_id: 1, num_devices: 1 })
});
const body = await response.json();
if (!response.ok) throw new Error(JSON.stringify(body.detail));
console.log(body.id, body.status);
```

## 错误处理

业务错误形如 `{"detail":"项目不存在"}`。字段校验错误的 `detail` 为数组，其中 `loc` 指出字段位置，`msg` 给出原因。

| 状态码 | 含义与处理 |
| --- | --- |
| `404` | 项目、任务、结果集或评测不存在 |
| `409` | 名称重复或当前状态不允许操作 |
| `422` | 缺少字段、空白命令、非法参数、无可满足约束的服务器、目录不可读、工程校验失败；修改参数后提交 |
| `502` | Agent 连接或调用失败，检查服务器详情的 `last_error` 与日志 |
| `503` / `504` | 文件或图片读取排队超时 / 读取超时；503 按 `Retry-After` 重试 |

评测执行失败通过评测对象的 `status=failed` 和 `error` 表达，此时查询本身仍返回 200；可用 `job_id` 查询任务日志。批量提交的 201 仅表示创建成功，不表示计算已完成。
