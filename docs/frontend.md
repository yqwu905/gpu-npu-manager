# 前端说明

前端在 `web/`，按设计原型（<https://claude.ai/artifact/QgNXZkh5JKpCac8gfZcYmE>）实现，共五个页面：总览、服务器、任务、结果与评测、对比。

技术栈：Vite + React 18 + TypeScript，路由用 HashRouter，不依赖外部字体和 CDN，可在内网使用。

## 开发与构建

```sh
cd web
npm ci
npm run dev      # http://localhost:5173/ui/ ，/api 代理到 127.0.0.1:8000
npm run build    # 产物在 web/dist
```

中心服务启动时，如果 `GNM_WEB_DIR`（默认 `web/dist`）下有 `index.html`，就在 `/ui/` 托管前端，并把 `/` 重定向到 `/ui/`。没有构建产物时只提供接口，不影响现有行为。

## 目录

```
web/src/api/       接口封装与类型（types.ts 与后端 schemas.py 字段一致）
web/src/mock/      后端尚未提供的接口的模拟数据
web/src/lib/       格式化、卡状态判定、轮询 hook
web/src/components 通用组件
web/src/pages/     五个页面
```

## 页面与接口对应

| 页面 | 使用的接口 | 状态 |
| --- | --- | --- |
| 总览 | `GET /api/overview`、`GET /api/servers`、`GET /api/jobs`、`GET /api/evaluations`、`GET /api/evaluators` | 已对接（评测部分按 PR #3） |
| 服务器 | `GET /api/servers`（全部筛选参数）、`/servers/grouped`、`/servers/filters`、`GET/PATCH/DELETE /servers/{id}`、`POST /servers`、`POST /servers/{id}/refresh`、`GET /servers/{id}/history` | 已对接 |
| 任务 | `GET/POST /api/jobs`、`GET/PATCH /jobs/{id}`、`POST /jobs/{id}/cancel?force=`、`POST /jobs/{id}/requeue`、`GET /jobs/{id}/log?offset=` | 已对接 |
| 结果与评测 | `GET/POST /api/results`、`GET /results/{id}/samples`、`GET /results/{id}/file`、`GET /api/evaluators`、`GET/POST /api/evaluations` | 按 PR #3 对接 |
| 对比 | `GET /api/compare/metrics`、`GET /api/compare/samples` | 按 PR #3 对接 |

### 模拟数据兜底

`api/client.ts` 的 `withFallback` 在接口返回 FastAPI 默认的 404（`{"detail": "Not Found"}`，即路由未注册）时，把该功能切换到 `web/src/mock` 下的模拟数据，页面上显示“示例数据 · xx接口未上线”标记。分三组独立判断：

- `jobs`：任务接口，PR #2 已合并，正常情况下不会走模拟数据。
- `scheduler`：调度模式接口（见缺口 1）。
- `results`：结果、评测、对比接口。PR #3 合并前走模拟数据，合并后自动切换为真实数据，不需要改前端。

业务上的 404（例如“服务器不存在”）的 detail 不是 `Not Found`，不会触发兜底。

### 卡状态判定

`lib/format.ts` 的 `deviceState()` 把每张卡归为：空闲、平台任务占用（`devices[].job_id` 非空）、外部占用、异常（health 不是 OK）、离线（服务器不在线）。`job_id` 字段来自 PR #2；第 1 步的接口没有这个字段时，非空闲卡统一显示为“占用”。

## 接口缺口

### 1. 调度模式查询与切换（第 2 步）

PR #2 只能用环境变量 `GNM_SCHEDULE_STRICT` 配置严格按序调度。任务页原型里有“严格按序”开关，侧栏也显示当前模式。建议增加：

```
GET   /api/scheduler           -> {"strict_order": false}
PATCH /api/scheduler  {"strict_order": true} -> {"strict_order": true}
```

运行期修改可以只存内存，重启后回到环境变量的值。接口缺失时前端隐藏开关，显示“调度模式由服务端配置”。

### 2. 排队原因（第 2 步）

任务详情和总览的排队列表需要显示任务为什么还在等，例如“组 ocr 空闲 1 张，需要 2 张”“前面有更高优先级任务”。建议在 `JobOut` 上增加可选字段：

```
wait_reason: str | None   # 仅 status=queued 时有值，由调度器在每轮扫描后写入
```

缺失时前端只显示队列位置。

### 结果与评测的对接方式（PR #3）

PR #3 的接口已经覆盖结果浏览、评测和多组对比，前端按它实现，没有新增后端需求。几处取舍：

- 结果集没有类型字段。样本浏览和对比页按样本字段决定展示方式：有 `text` 按文字展示（识别结果、参考答案、错字高亮），否则有 `image` 按图片网格展示，其他字段作为附加列原样显示。发起评测时读第一条样本，默认勾选对应类型的指标。
- 列表的指标列只显示至少一个结果集有值的指标，显示名、单位、方向都来自 `GET /api/evaluators`。
- 对比页可以任选基线。接口的样本顺序和 `asc`/`desc` 排序以第一个结果集为准，所以请求时把基线放在 `ids` 的第一位；“基线最差优先”按指标方向换成 `asc` 或 `desc`。
- 参考图用样本里的 `ref_image`（可以是服务器上的绝对路径），通过同一个 `/file` 接口读取。
- 样本浏览不再提供按 id 搜索，接口没有这个参数。数据量大时如果需要，可以给 `/samples` 加一个 `q`。

用 PR #3 分支的中心服务和 Agent 实测过：登记 2 组图像、2 组 OCR 结果，发起 PSNR、SSIM、LPIPS、OCR-A、CER、1-NED 评测，列表、样本浏览、评测记录（含 LPIPS 未安装时的单项错误）、图像和文字对比都显示正常。

## 合并注意

本次在 `server/app/main.py` 增加一行 `mount_web(...)` 和一行 import，新增 `server/app/web.py`，`config.py` 增加 `web_dir` 字段，没有改动任何接口。和 PR #3 合并时，`main.py` 的 import 行和 README 的环境变量表可能各有一处文本冲突，保留两边即可。
