# 接口说明（服务器与卡状态、任务调度、推理结果与评测）

供前端设计和对接使用。完整字段定义见 [openapi.json](openapi.json)，服务启动后也可以访问 `http://<中心服务>/docs` 在线调试。

- 所有接口前缀为 `/api`，返回 JSON，时间均为 UTC ISO 8601 字符串。
- Web 端不需要登录。
- 多值查询参数用重复参数传递，例如 `?tag=a100&tag=lab1`。

## 页面与接口对应

| 页面 | 用到的接口 |
| --- | --- |
| 首页总览 | `GET /api/overview` |
| 服务器列表（筛选） | `GET /api/servers`、`GET /api/meta/filters` |
| 服务器分组视图 | `GET /api/servers/grouped?by=group\|owner\|tag\|accelerator\|model\|status` |
| 服务器详情 | `GET /api/servers/{id}`、`GET /api/servers/{id}/history`、`POST /api/servers/{id}/refresh` |
| 添加 / 编辑服务器 | `POST /api/servers`、`POST /api/servers/batch`、`GET /api/ssh-config`、`GET /api/agent-package`、`PATCH /api/servers/{id}`、`DELETE /api/servers/{id}` |
| 安装 / 升级 Agent | `POST /api/servers/{id}/deploy`、`POST /api/servers/deploy`、`GET /api/servers/{id}/deploy` |
| 任务队列 / 任务列表 | `GET /api/jobs`、`GET /api/scheduler`、`PATCH /api/scheduler` |
| 提交任务 | `POST /api/jobs`（运行组候选值来自 `GET /api/meta/filters`） |
| 结果集列表 / 登记 | `GET /api/results`、`POST /api/results` |
| 结果集详情（样本浏览） | `GET /api/results/{id}`、`GET /api/results/{id}/samples`、`GET /api/results/{id}/file`、`GET /api/evaluations?result_set_id=` |
| 发起评测 | `GET /api/evaluators`、`POST /api/evaluations` |
| 指标对比 | `GET /api/compare/metrics`、`GET /api/compare/samples` |
| 任务详情与日志 | `GET /api/jobs/{id}`、`GET /api/jobs/{id}/log`、`POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/requeue`、`PATCH /api/jobs/{id}` |

## 接口列表

### GET /api/overview

首页汇总数字。

```json
{
  "servers_total": 12, "servers_online": 11, "servers_offline": 1,
  "devices_total": 96, "devices_idle": 30, "devices_busy": 66,
  "by_accelerator": {
    "gpu": {"servers": 5, "devices": 40, "idle_devices": 12},
    "npu": {"servers": 7, "devices": 56, "idle_devices": 18}
  },
  "jobs_queued": 5, "jobs_running": 9, "jobs_lost": 0
}
```

### GET /api/servers

服务器列表，按名称排序。查询参数全部可选：

| 参数 | 说明 |
| --- | --- |
| `q` | 关键字，匹配名称、地址、主机名、备注（不区分大小写） |
| `group` | 运行组，可多个，满足其一即可 |
| `owner` | 使用人，可多个，满足其一即可 |
| `tag` | 标签，可多个，必须同时包含 |
| `accelerator` | `gpu` 或 `npu` |
| `model` | 卡型号，可多个，满足其一即可 |
| `status` | `online` / `offline` / `unknown`（刚添加、尚未连通） |
| `schedulable` | 是否参与调度 |
| `has_idle` | `true` 只看有空闲卡的服务器 |
| `include_devices` | 默认 `true`；列表页不需要每张卡详情时传 `false` 减少数据量 |

单个服务器对象（`ServerOut`）：

```json
{
  "id": 1, "name": "npu-01", "host": "10.0.0.11", "port": 9100,
  "group": "cv", "owner": "alice", "tags": ["910B", "lab1"], "note": null, "schedulable": true,
  "ssh_user": "alice", "ssh_port": 22, "ssh_host": null, "ssh_tunnel": true,
  "accelerator": "npu", "status": "online", "hostname": "node11", "agent_version": "0.1.0+aabf8f791b",
  "agent_outdated": false, "managed": true,
  "deploy": {"status": "succeeded", "action": "install", "version": "0.1.0+aabf8f791b", "error": null,
             "started_at": "2026-10-08T14:00:01Z", "finished_at": "2026-10-08T14:00:09Z"},
  "host_info": {
    "cpu_count": 192, "cpu_percent": 12.5, "load1": 3.2,
    "memory_total_mb": 1546000, "memory_used_mb": 210000,
    "disks": [{"path": "/", "total_gb": 1800, "used_gb": 640}]
  },
  "last_seen_at": "2026-10-08T14:06:32Z", "last_error": null,
  "models": ["910B2"], "device_count": 8, "idle_device_count": 3,
  "devices": [
    {
      "index": 0, "vendor": "ascend", "model": "910B2", "npu_id": 0, "chip_id": 0,
      "bus_id": "0000:C1:00.0", "uuid": null,
      "memory_total_mb": 65536, "memory_used_mb": 3862,
      "utilization": 0, "temperature": 43, "power_w": 107.3, "power_limit_w": null,
      "health": "OK", "health_detail": null, "idle": false, "job_id": 42,
      "processes": [{"pid": 218169, "name": "ray_RayTrainW", "user": "alice", "memory_mb": 558}],
      "updated_at": "2026-10-08T14:06:32Z"
    }
  ],
  "created_at": "...", "updated_at": "..."
}
```

字段说明：

- `status`：连续 3 次拉取失败判定为 `offline`，`last_error` 给出最后一次错误原因。
- `ssh_host`：SSH 连接目标，可以是中心主机 `~/.ssh/config` 中的 Host 别名（沿用其中的密钥、跳板机等设置）；为 `null` 时用 `host`。
- `ssh_tunnel`：默认 `true`，中心服务通过 SSH 端口转发访问 Agent（Agent 只监听服务器本机，防火墙不需要放通 Agent 端口）；只对填写了 `ssh_user` 的服务器生效。改为 `false` 后需要重新安装 Agent，让它对外监听。转发建立失败时 `last_error` 以“SSH 转发失败”开头。
- `managed`：填写了 `ssh_user`，由中心服务通过 SSH 安装和升级 Agent；任务以该用户运行。
- `agent_version`：Agent 版本，`+` 后面是 agent.py 和 evaluate.py 的内容摘要；`agent_outdated` 表示与中心服务自带的版本不一致。
- `deploy`：最近一次安装或升级，从未通过 SSH 部署过时为 `null`。`status` 为 `pending` / `running` / `succeeded` / `failed`，`action` 为 `install` / `upgrade`，失败原因在 `error`。
- `devices[].index`：逻辑卡号，任务调度时就是 `CUDA_VISIBLE_DEVICES` / `ASCEND_RT_VISIBLE_DEVICES` 的值。
- `devices[].utilization`：GPU 为 GPU 利用率，NPU 为 AICore 利用率，单位 %。
- `devices[].memory_*`：GPU 为显存，910B 为 HBM，310P 为板载内存，单位 MB。
- `devices[].idle`：服务器在线、健康为 OK、没有进程、已用显存低于阈值（GPU 默认 1 GB，NPU 默认 6 GB），且没有被平台任务占用。
- `devices[].job_id`：占用这张卡的平台任务 ID，没有则为 `null`。
- 取不到的数值为 `null`（例如部分型号不提供功耗），前端显示为 “-”。

### GET /api/servers/grouped?by=...

返回分组数组，查询参数与 `/api/servers` 相同，另需 `by`：`group`、`owner`、`tag`、`accelerator`、`model`、`status`。按标签或型号分组时，一台服务器可能出现在多个分组里。`key` 为 `null` 的分组表示未设置该属性，排在最后。

```json
[
  {"key": "cv", "server_count": 4, "online_count": 4, "device_count": 32, "idle_device_count": 9, "servers": [ServerOut, ...]},
  {"key": null, "server_count": 1, "online_count": 0, "device_count": 0, "idle_device_count": 0, "servers": [...]}
]
```

### GET /api/meta/filters

筛选下拉框的候选值。

```json
{"groups": ["cv", "nlp"], "owners": ["alice", "bob"], "tags": ["910B", "lab1"],
 "models": ["910B2", "NVIDIA A100-SXM4-80GB"], "accelerators": ["gpu", "npu"]}
```

### POST /api/servers?deploy=true

添加服务器。

```json
{"name": "npu-01", "host": "10.0.0.11", "port": 9100, "ssh_user": "alice", "ssh_port": 22,
 "group": "cv", "owner": "alice", "tags": ["910B"], "note": "", "schedulable": true}
```

只有 `host` 必填；`name` 不填时用地址，`port`（Agent 端口）默认 9100。填写 `ssh_user` 时，添加后立即通过 SSH 安装 Agent（`?deploy=false` 只登记不安装）；不填表示 Agent 已手动安装，添加后立即尝试连接一次。名称重复返回 409。

### POST /api/servers/batch

批量添加，请求体 `{"servers": [与上面相同的对象, ...], "deploy": true}`，最多 200 台。名称重复的条目跳过，其余照常添加：

```json
{"created": [ServerOut, ...], "errors": [{"index": 2, "host": "10.0.0.13", "error": "名称 npu-03 已存在"}]}
```

### GET /api/ssh-config

列出中心主机 `~/.ssh/config`（含 Include 的文件）中不含通配符的 Host，按 OpenSSH 规则取生效的地址、用户和端口（每个选项取第一个匹配的值，`Host *` 只补充前面没设置的；没有 User 时为中心服务的运行用户）。

```json
{"path": "/root/.ssh/config", "hosts": [
  {"alias": "gpu-01", "hostname": "10.0.1.21", "user": "alice", "port": 22, "added": false}
]}
```

`added` 表示名称、地址或 SSH 目标与已添加的服务器相同。导入时用 `{"name": alias, "host": hostname, "ssh_host": alias, "ssh_user": user, "ssh_port": port}` 调用批量添加。

### GET /api/agent-package

中心服务自带的 Agent 版本和 SSH 准备情况，用于添加对话框里的提示。

```json
{"version": "0.1.0+aabf8f791b", "ssh_available": true, "public_key": "ssh-ed25519 AAAA... root@center",
 "public_key_path": "/root/.ssh/id_ed25519.pub", "auto_upgrade": true}
```

`public_key` 需要加入各服务器 SSH 用户的 `~/.ssh/authorized_keys`；中心主机还没有密钥时为 `null`。

### POST /api/servers/{id}/deploy

在后台安装或升级该服务器的 Agent，立即返回 `ServerOut`（`deploy.status` 为 `pending`）。没有填写 `ssh_user` 或正在安装时返回 409。安装过程：SSH 登录（密钥免密），把 Agent 写到 `~/.gnm-agent/bin/`，停掉旧进程后启动，再等待 Agent 响应并核对版本。升级只重启 Agent 进程，运行中的任务不受影响。

### POST /api/servers/deploy

批量安装或升级。`{"server_ids": [1, 2]}` 指定服务器，或 `{"outdated": true}` 选择所有版本落后或尚未安装的托管服务器。返回实际开始部署的 `ServerOut` 列表（跳过没有 SSH 信息和正在安装的）。

### GET /api/servers/{id}/deploy

最近一次安装或升级的详情，比 `ServerOut.deploy` 多一个 `log` 字段（SSH 执行输出，最后 20000 个字符）；从未部署过时返回 `null`。部署进行中时前端可以每 2 秒轮询一次。

### PATCH /api/servers/{id}

只提交要修改的字段，例如 `{"owner": "bob", "tags": ["a100"]}`。`group`、`owner`、`note`、`ssh_user` 可以设为 `null` 清空。修改 `ssh_user`、`port` 后需要重新安装 Agent 才生效。名称重复返回 409。

### DELETE /api/servers/{id}

删除服务器及其历史数据，返回 204。服务器上还有运行中的任务时返回 409。

### POST /api/servers/{id}/refresh

立即拉取一次该服务器状态并返回最新的 `ServerOut`，用于详情页的“刷新”按钮。

### GET /api/servers/{id}/history?hours=24

最近若干小时（1 到 168）的趋势数据，每张卡每分钟一个点，保留 7 天。

```json
[
  {"device_index": 0, "points": [{"ts": "2026-10-08T14:00:00Z", "utilization": 87, "memory_used_mb": 52000, "temperature": 61, "power_w": 310.5}]}
]
```

## 任务

任务是在单台服务器上运行的一条 shell 命令（以 Agent 运行用户的 `bash -l` 执行）。调度器每 5 秒扫描一次队列：按优先级从高到低、提交时间从早到晚，为每个任务在满足条件的服务器中找足够的空闲卡；多台都满足时选空闲卡最少的那台，减少碎片。排在前面的任务放不下时，默认允许后面的小任务先运行（回填）；切换为严格按序后，前面的任务放不下时后面的任务也不调度（见 `/api/scheduler`）。

分配到的卡号通过 `CUDA_VISIBLE_DEVICES`（GPU）或 `ASCEND_RT_VISIBLE_DEVICES`（NPU）传给进程，另有 `GNM_JOB_ID` 环境变量。

### 状态

| 状态 | 含义 | 可做的操作 |
| --- | --- | --- |
| `queued` | 排队中，`queue_position` 为队列位置，`wait_reason` 为等待原因 | 取消、改优先级 |
| `starting` | 已分配卡，正在启动 | 取消 |
| `running` | 运行中 | 取消、看日志 |
| `lost` | 服务器离线，失联；恢复后自动对账 | 取消（需 `force=true`） |
| `succeeded` | 退出码 0 | 重新排队、看日志 |
| `failed` | 退出码非 0、被信号终止或启动被拒绝，原因见 `error` | 重新排队、看日志 |
| `cancelled` | 已取消 | 重新排队、看日志 |

### POST /api/jobs

```json
{
  "name": "resnet50-train",
  "command": "python train.py --epochs 10",
  "workdir": "/home/alice/project",
  "env": {"BATCH_SIZE": "64"},
  "num_devices": 2,
  "group": "cv",
  "accelerator": null,
  "server_id": null,
  "priority": 0,
  "submitter": "alice"
}
```

只有 `command` 必填。`name` 不填时取命令第一行；`num_devices` 默认 1，可以为 0（不需要卡）；`group`、`accelerator`（`gpu`/`npu`）、`server_id` 为空表示不限；`priority` 范围 -100 到 100，越大越先调度。没有任何服务器能满足条件（运行组、类型、卡数）时返回 422。

返回 `JobOut`。排队中的任务 `wait_reason` 说明为什么还没被调度，每轮调度更新，刚提交还没经过一轮调度时为 `null`，例如：

- `组 cv：单台服务器最多空闲 3 张卡，需要 4 张`
- `组 nlp：没有在线且可调度的服务器`
- `严格按序调度，等待前面的任务 #41（resnet50-train）先启动`


```json
{
  "id": 42, "name": "resnet50-train", "command": "python train.py --epochs 10",
  "workdir": "/home/alice/project", "env": {"BATCH_SIZE": "64"},
  "num_devices": 2, "group": "cv", "accelerator": null, "server_id": null,
  "priority": 0, "submitter": "alice",
  "status": "running", "queue_position": null, "wait_reason": null,
  "assigned_server_id": 3, "assigned_server_name": "gpu-03", "device_indices": [0, 1],
  "pid": 12345, "exit_code": null, "error": null, "requeued_from": null,
  "created_at": "2026-10-08T14:00:00Z", "started_at": "2026-10-08T14:00:05Z", "finished_at": null
}
```

### GET /api/jobs

返回 `{"total": 123, "items": [JobOut, ...]}`，默认按 ID 倒序；只筛选 `status=queued` 时按调度顺序返回。

| 参数 | 说明 |
| --- | --- |
| `status` | 状态，可多个，例如 `?status=queued&status=running` |
| `group` | 运行组 |
| `submitter` | 提交人 |
| `server_id` | 实际运行的服务器 |
| `q` | 关键字，匹配名称和命令 |
| `limit` / `offset` | 分页，默认 50 / 0 |

### GET /api/jobs/{id}

任务详情，返回 `JobOut`。

### PATCH /api/jobs/{id}

修改 `name` 或 `priority`。只有排队中的任务可以改优先级，否则返回 409。

### POST /api/jobs/{id}/cancel

取消任务。排队中的任务直接取消；已启动的任务会终止整个进程组（先 SIGTERM，10 秒后仍未退出则 SIGKILL）。Agent 连不上时返回 502，可加 `?force=true` 强制标记为已取消（进程可能仍在运行）。已结束的任务返回 409。

### POST /api/jobs/{id}/requeue

以相同参数创建一个新任务（`requeued_from` 指向原任务），只能对已结束的任务操作，返回新任务。

### GET /api/scheduler

查询调度模式，返回 `{"strict_order": false}`。`false` 允许回填，`true` 严格按队列顺序调度。

### PATCH /api/scheduler

切换调度模式，请求体 `{"strict_order": true}`，返回切换后的值，下一轮调度生效。只保存在内存中，中心服务重启后恢复为环境变量 `GNM_SCHEDULE_STRICT` 的值。

### GET /api/jobs/{id}/log?offset=0&limit=65536

增量读取日志，返回 `{"offset": 0, "next_offset": 2048, "size": 2048, "data": "..."}`。首次传 `offset=0`，之后传上次的 `next_offset`，可以实现滚动追加。还未启动的任务返回空内容。

## 推理结果与评测

结果集是某台服务器上的一个目录（格式见仓库 README 的“推理结果格式”），平台通过 Agent 读取，文件留在原服务器上。评测会作为普通任务提交到评测服务器上运行（未指定时为结果所在的服务器；任务名为“评测 <结果集名>”，在任务列表中也能看到），输出写到该服务器上结果目录下的 `eval/<评测 ID>/`。

### 项目：GET/POST /api/projects，PATCH/DELETE /api/projects/{id}

`POST {"name": "超分", "description": "可选"}`，名称不能重复（409）。返回 `{"id", "name", "description", "result_count", "created_at"}`。删除项目时其中的结果集改为未归档，不会删除。

### 评测配置：GET/POST /api/eval-configs，PATCH/DELETE /api/eval-configs/{id}

```json
{"name": "DIV2K ×4", "metrics": ["psnr", "ssim", "ocr_a"],
 "label_file": "/data/ds/Label.txt", "gt_dir": "/data/ds/HR", "lq_dir": "/data/ds/LR",
 "server_id": 5, "server_path": "/data/eval", "num_devices": 1, "python": "/data/envs/eval/bin/python", "note": null}
```

`python` 是在评测服务器上运行评测脚本的解释器，例如装了 torch、lpips、paddleocr 的 venv 中的 `bin/python`；为空时用中心服务的 `GNM_EVAL_PYTHON`（默认 `python3`）。

路径都在评测服务器上（`server_id` 为空时在结果所在服务器上），必须是绝对路径，空串视为未填。指定 `server_id` 时 `server_path` 必填，且该服务器要填写 SSH 用户。返回值另有 `id`、`server_name`、`created_at`、`updated_at`。修改和删除配置不影响已有评测。

### GET /api/evaluators

可选的指标及展示信息，`higher_is_better` 用于在对比表中标出更好的一方。

```json
[
  {"name": "psnr", "label": "PSNR", "kind": "image", "unit": "dB", "higher_is_better": true, "description": "..."},
  {"name": "ssim", "label": "SSIM", "kind": "image", "unit": null, "higher_is_better": true, "description": "..."},
  {"name": "lpips", "label": "LPIPS", "kind": "image", "unit": null, "higher_is_better": false, "description": "..."},
  {"name": "ocr_a", "label": "OCR-A", "kind": "text", "unit": null, "higher_is_better": true, "description": "..."},
  {"name": "cer", "label": "CER", "kind": "text", "unit": null, "higher_is_better": false, "description": "..."},
  {"name": "ned", "label": "1-NED", "kind": "text", "unit": null, "higher_is_better": true, "description": "..."}
]
```

### POST /api/results

登记一个结果集：`{"server_id": 3, "path": "/data/results/model-a", "name": "可选", "note": "可选", "job_id": null, "project_id": null, "tags": ["v1"]}`。平台会读取目录下的 `meta.json`（可选）并统计样本数：有 `predictions.jsonl` 时按它统计，没有时扫描目录里的图片和 `.txt`（见 README“推理结果格式”）；目录无法读取或没有任何样本时返回 422。

返回 `ResultSetOut`：

```json
{
  "id": 7, "name": "model-a", "server_id": 3, "server_name": "gpu-03",
  "path": "/data/results/model-a",
  "meta": {"model": "model-a", "dataset": "demo"},
  "sample_count": 1000, "note": null, "job_id": null,
  "project_id": 2, "project_name": "超分", "tags": ["v1"],
  "metrics": {"psnr": 28.41, "ssim": 0.873, "ocr_a": 0.92, "cer": 0.031, "ned": 0.975},
  "lq_metrics": {"psnr": 24.9, "ssim": 0.70},
  "evaluating": false,
  "created_at": "2026-10-08T15:00:00Z"
}
```

`metrics` 是每个指标最近一次成功评测的值；还没评测过时为空对象。`lq_metrics` 是最近一次带 LQ 基线的评测对应的 LQ 指标。

### GET /api/results

结果集列表，按登记时间倒序，可用 `server_id`、`project_id`（0 表示未归档）、`tag`（可重复，需全部包含）、`q`（匹配名称、路径、备注）筛选。`GET /api/results/{id}` 返回单个；`PATCH` 可改 `name`、`note`、`project_id`（null 表示移出项目）、`tags`；`DELETE` 只删除登记，不删服务器上的文件。`GET /api/results/tags` 返回用到的所有标签及结果集数 `[{"tag": "v1", "count": 3}]`。

### GET /api/results/{id}/samples?offset=0&limit=50

分页浏览样本，返回 `{"total": 1000, "offset": 0, "items": [...]}`。每个 item 是 `predictions.jsonl` 中的原始记录（没有它时为扫描得到的 `id`、`image`、`text`），另加 `metrics`（该样本的逐样本指标）：

```json
{"id": "0001", "image": "images/0001.png", "ref_image": "/data/gt/0001.png",
 "text": "识别结果", "ref_text": "真实文本",
 "metrics": {"psnr": 27.9, "ssim": 0.86, "ocr_a": 0.0, "cer": 0.25, "ned": 0.75}}
```

最近一次成功评测配对到的 `ref_image`、`ref_text`、`lq_image`（LQ 图片）和 `ocr_text`（OCR 识别文本）在记录没有该字段时补上。补上的图片在评测服务器上，`media` 给出来源：`{"lq_image": 12, "ref_image": 12}`（字段名 -> 评测 ID），读取时在 file 接口加 `evaluation_id`。

### GET /api/results/{id}/file?path=images/0001.png

读取结果集里的文件（主要是图片），直接返回文件内容和对应的 Content-Type，可以作为 `<img src>` 使用。`path` 可以是相对结果集目录的路径，也可以是服务器上的绝对路径（如参考图）。加 `evaluation_id` 时从该评测的评测服务器读取（相对路径相对于拷贝过去的结果目录）。

### POST /api/evaluations

```json
{"result_set_id": 7, "metrics": ["psnr", "ssim", "ocr_a", "cer", "ned"], "reference": null, "num_devices": 0, "priority": 0, "submitter": "alice"}
```

也可以用评测配置：`{"result_set_id": 7, "config_id": 3}`，请求里另外给出的 `metrics`、`label_file`、`gt_dir`、`lq_dir`、`server_id`、`server_path`、`num_devices`、`python` 覆盖配置中的值。评测服务器与结果所在服务器不同时，先把结果目录拷贝到 `server_path/result_<结果集 ID>`（状态 `copying`），完成后再提交评测任务；结果所在服务器没有填写 SSH 用户时返回 422。

`reference` 可选，是服务器上的参考目录（按文件名配对图片和 `.txt`）、参考值 jsonl，或 PaddleOCR 格式的文字标注文件（每行“图片文件名<Tab>文本框 JSON 数组”，见 README）的路径。样本没有识别文本时，文字指标会先用 PaddleOCR 识别预测图。`num_devices` 默认 0；LPIPS 和 OCR 可以给 1 张卡加速。返回 `EvaluationOut`：

```json
{
  "id": 12, "result_set_id": 7, "result_set_name": "model-a",
  "metrics": ["psnr", "ssim", "ocr_a", "cer", "ned"], "reference": null,
  "config_id": 3, "config_name": "DIV2K ×4", "label_file": null, "gt_dir": "/data/ds/HR", "lq_dir": "/data/ds/LR",
  "python": "/data/envs/eval/bin/python",
  "server_id": 5, "server_name": "gpu-05", "data_path": "/data/eval/result_7", "output_dir": "/data/eval/result_7/eval/12",
  "compute_lq": true, "lq_source_id": 12, "lq_values": {"psnr": 24.9, "ssim": 0.70}, "lq_counts": {"psnr": 1000, "ssim": 1000}, "lq_errors": null,
  "job_id": 88, "job_status": "succeeded",
  "status": "succeeded",
  "values": {"psnr": 28.41, "ssim": 0.873, "ocr_a": 0.92, "cer": 0.031, "ned": 0.975},
  "counts": {"psnr": 1000, "ssim": 1000, "ocr_a": 1000, "cer": 1000, "ned": 1000},
  "errors": null, "num_skipped": 0, "error": null,
  "created_at": "...", "finished_at": "..."
}
```

`compute_lq` 表示本次是否计算 LQ 基线：同一配置下 LQ、GT、Label、评测服务器都相同且指标覆盖本次的评测已经算过（或正在算）时为 false，`lq_source_id` 指向那次评测，`lq_values` 取自它。

`status`：`copying`（正在拷贝到评测服务器）/ `pending`（任务排队中）/ `running` / `succeeded` / `failed`（原因见 `error`，详细输出看 `job_id` 对应任务的日志）。评测成功但个别指标算不出来（如服务器没装 lpips、缺少 `ref_text` 字段）时，该指标在 `values` 中为 `null`，原因在 `errors` 中。

`GET /api/evaluations?result_set_id=7` 列出某个结果集的评测历史，`GET /api/evaluations/{id}` 返回单个。

### GET /api/compare/metrics?ids=7&ids=8&ids=9

多组结果的指标对比：

```json
{
  "result_sets": [{"id": 7, "name": "model-a", "server_name": "gpu-03", "meta": {}}, {"id": 8, "name": "model-b", "server_name": "npu-01", "meta": {}}],
  "metrics": [EvaluatorOut, ...],
  "values": {"7": {"psnr": 28.41, "cer": 0.031}, "8": {"psnr": 26.02, "cer": 0.054}},
  "lq_values": {"7": {"psnr": 24.9}}
}
```

`lq_values` 是有 LQ 基线的结果集的 LQ 指标（同一配置下相同）。

`metrics` 只包含至少一个结果集有值的指标，顺序固定；某个结果集缺少某指标时 `values` 里没有该键。

### GET /api/compare/samples?ids=7&ids=8&sort_metric=psnr&sort=spread&offset=0&limit=20

按样本 `id` 对齐，并排展示多组结果的同一样本，样本顺序以第一个结果集为准：

```json
{
  "total": 1000, "offset": 0,
  "items": [
    {"id": "0042", "spread": 6.3,
     "results": {"7": {"id": "0042", "image": "images/0042.png", "text": "...", "metrics": {"psnr": 30.1}},
                 "8": {"id": "0042", "image": "images/0042.png", "text": "...", "metrics": {"psnr": 23.8}}}}
  ]
}
```

- `sort_metric` 不填时按原顺序；填了且 `sort=spread`（默认）时，按该指标在各结果集之间的差值从大到小排列，方便找出差别最大的样本；`sort=asc`/`desc` 按第一个结果集的值排序。
- 某结果集没有该样本时，对应值为 `null`。
- 图片用 `/api/results/{结果集 ID}/file?path=<image 字段>` 读取。
- 单个结果集超过 50000 个样本时返回 422。

## 刷新频率

中心服务每 10 秒拉取一次各服务器状态，每 5 秒调度一次。前端列表和详情页每 5 到 10 秒轮询一次即可，运行中任务的日志可以每 2 秒增量拉取。
