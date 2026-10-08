# 接口说明（第 1 步：服务器与卡状态）

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
| 添加 / 编辑服务器 | `POST /api/servers`、`PATCH /api/servers/{id}`、`DELETE /api/servers/{id}` |

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
  }
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
  "accelerator": "npu", "status": "online", "hostname": "node11", "agent_version": "0.1.0",
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
      "health": "OK", "idle": false,
      "processes": [{"pid": 218169, "name": "ray_RayTrainW", "user": "alice", "memory_mb": 558}],
      "updated_at": "2026-10-08T14:06:32Z"
    }
  ],
  "created_at": "...", "updated_at": "..."
}
```

字段说明：

- `status`：连续 3 次拉取失败判定为 `offline`，`last_error` 给出最后一次错误原因。
- `devices[].index`：逻辑卡号，任务调度时就是 `CUDA_VISIBLE_DEVICES` / `ASCEND_RT_VISIBLE_DEVICES` 的值。
- `devices[].utilization`：GPU 为 GPU 利用率，NPU 为 AICore 利用率，单位 %。
- `devices[].memory_*`：GPU 为显存，910B 为 HBM，310P 为板载内存，单位 MB。
- `devices[].idle`：服务器在线、健康为 OK、没有进程、已用显存低于阈值（GPU 默认 1 GB，NPU 默认 6 GB）。
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

### POST /api/servers

添加服务器，添加后会立即尝试连接一次。

```json
{"name": "npu-01", "host": "10.0.0.11", "port": 9100, "group": "cv", "owner": "alice", "tags": ["910B"], "note": "", "schedulable": true}
```

`name` 和 `host` 必填，`port` 默认 9100。名称重复返回 409。

### PATCH /api/servers/{id}

只提交要修改的字段，例如 `{"owner": "bob", "tags": ["a100"]}`。`group`、`owner`、`note` 可以设为 `null` 清空。名称重复返回 409。

### DELETE /api/servers/{id}

删除服务器及其历史数据，返回 204。

### POST /api/servers/{id}/refresh

立即拉取一次该服务器状态并返回最新的 `ServerOut`，用于详情页的“刷新”按钮。

### GET /api/servers/{id}/history?hours=24

最近若干小时（1 到 168）的趋势数据，每张卡每分钟一个点，保留 7 天。

```json
[
  {"device_index": 0, "points": [{"ts": "2026-10-08T14:00:00Z", "utilization": 87, "memory_used_mb": 52000, "temperature": 61, "power_w": 310.5}]}
]
```

## 刷新频率

中心服务每 10 秒拉取一次各服务器状态，前端列表和详情页每 10 秒轮询一次即可。
