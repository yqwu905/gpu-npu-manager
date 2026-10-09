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
web/src/pages/     五个页面（对比页的图片对比视图在 CompareImages.tsx）
```

## 页面与接口对应

| 页面 | 使用的接口 | 状态 |
| --- | --- | --- |
| 总览 | `GET /api/overview`、`GET /api/servers`、`GET /api/jobs`、`GET /api/evaluations`、`GET /api/evaluators` | 已对接 |
| 服务器 | `GET /api/servers`（全部筛选参数）、`/servers/grouped`、`/servers/filters`、`GET/PATCH/DELETE /servers/{id}`、`POST /servers`、`POST /servers/{id}/refresh`、`GET /servers/{id}/history`、`POST /servers/batch`、`GET /agent-package`、`POST /servers/{id}/deploy`、`POST /servers/deploy`、`GET /servers/{id}/deploy` | 已对接 |
| 任务 | `GET/POST /api/jobs`、`GET/PATCH /jobs/{id}`、`POST /jobs/{id}/cancel?force=`、`POST /jobs/{id}/requeue`、`GET /jobs/{id}/log?offset=`、`GET/PATCH /api/scheduler` | 已对接 |
| 结果与评测 | `GET/POST /api/results`、`GET /results/{id}/samples`、`GET /results/{id}/file`、`GET /api/evaluators`、`GET/POST /api/evaluations` | 已对接 |
| 对比 | `GET /api/compare/metrics`、`GET /api/compare/samples`；图片对比视图另用 `GET /results/{id}/samples`、`GET /results/{id}/file` | 已对接 |

### 模拟数据兜底

`api/client.ts` 的 `withFallback` 在接口返回 FastAPI 默认的 404（`{"detail": "Not Found"}`，即路由未注册）时，把该功能切换到 `web/src/mock` 下的模拟数据，页面上显示“示例数据 · xx接口未上线”标记。分三组独立判断：

- `jobs`：任务接口。
- `scheduler`：调度模式接口。
- `results`：结果、评测、对比接口。

业务上的 404（例如“服务器不存在”）的 detail 不是 `Not Found`，不会触发兜底。

### 卡状态判定

`lib/format.ts` 的 `deviceState()` 把每张卡归为：空闲、平台任务占用（`devices[].job_id` 非空）、外部占用、异常（health 不是 OK）、离线（服务器不在线）。`job_id` 字段来自第 2 步；更早版本的接口没有这个字段时，非空闲卡统一显示为“占用”。

## 对接说明

### 调度模式与排队原因

- 任务页的“严格按序”开关对接 `GET/PATCH /api/scheduler`，侧栏显示当前模式。切换只保存在中心服务内存中，重启后回到 `GNM_SCHEDULE_STRICT` 的值。
- 任务详情和总览的排队列表显示 `JobOut.wait_reason`（为空时只显示队列位置）。
- 后端没有 `/api/scheduler` 时，前端隐藏开关，显示“调度模式由服务端配置”。

### 结果与评测的对接方式

第 3 步的接口已经覆盖结果浏览、评测和多组对比，前端按它实现，没有新增后端需求。几处取舍：

- 结果集没有类型字段。样本浏览和对比页按样本字段决定展示方式：有 `text` 按文字展示（识别结果、参考答案、错字高亮），否则有 `image` 按图片网格展示，其他字段作为附加列原样显示。发起评测时读第一条样本，默认勾选对应类型的指标。
- 列表的指标列只显示至少一个结果集有值的指标，显示名、单位、方向都来自 `GET /api/evaluators`。
- 对比页可以任选基线。接口的样本顺序和 `asc`/`desc` 排序以第一个结果集为准，所以请求时把基线放在 `ids` 的第一位；“基线最差优先”按指标方向换成 `asc` 或 `desc`。
- 参考图用样本里的 `ref_image`（可以是服务器上的绝对路径），通过同一个 `/file` 接口读取。
- 样本浏览不再提供按 id 搜索，接口没有这个参数。数据量大时如果需要，可以给 `/samples` 加一个 `q`。

用第 3 步的中心服务和 Agent 实测过：登记 2 组图像、2 组 OCR 结果，发起 PSNR、SSIM、LPIPS、OCR-A、CER、1-NED 评测，列表、样本浏览、评测记录（含 LPIPS 未安装时的单项错误）、图像和文字对比都显示正常。

### 对比页

设计稿：<https://claude.ai/artifact/QZqFpCQxLGL8R5HFNCYxY3>。代码在 `pages/Compare.tsx`（布局、推理结果选择、指标视图）和 `pages/CompareImages.tsx`（图片对比视图）。

进入对比页时左侧导航自动折叠成图标栏，鼠标悬停或键盘聚焦时浮层展开，不挤压内容；其他页面不变。手机宽度下导航照常横排。

页面从左到右分三栏，右上角可在“指标”和“图片对比”两个视图间切换（默认图片对比）：

1. **推理结果**：列出全部结果集，勾选的排在前面，可按名称筛选。勾选结果写入地址栏的 `ids`，两个视图共用。
2. **缩略图**（仅图片对比视图）：每个勾选的结果集一列，列出它的全部图片，按文件名字母序排列，当前显示的那张用该结果的颜色描边。
3. **主区域**：指标视图是原来的指标表、点图和逐样本对比；图片对比视图每个结果集只显示一张图，多个结果组成网格（1 组 1 列，2–4 组 2 列，更多 3 列）。

推理结果栏和缩略图栏都可以手动折叠成竖条，折叠和展开带宽度过渡和淡入淡出动画。

#### 图片列表

图片对比视图对每个勾选的结果集分页读取全部样本（`GET /results/{id}/samples`，每页 500 条），取样本的 `image` 字段，按文件名（不含目录）字母序排序，同名时按完整路径排序。读过的结果集在本页内缓存，取消再勾选不会重新读取。图片通过 `GET /results/{id}/file` 读取。

#### 切换与匹配

修饰键在 Mac 上是 ⌘（Ctrl+左键在 Mac 上会被系统当作右键），其他系统是 Ctrl，下表记作 Mod。

| 操作 | 效果 |
| --- | --- |
| 单击缩略图 | 只切换这一列对应的结果集，其他结果不变 |
| Mod + 单击缩略图 | 所有结果集切到同一序号；某个结果集图片较少时停在它的最后一张 |
| Alt + 单击缩略图 | 被点的结果集切到这张图，其他结果集各自选文件名编辑距离最小的图片（相同距离取排序靠前的） |
| 上一张 / 下一张按钮，← ↑ / → ↓ | 所有结果集各自前进或后退一张，保持当前对齐关系；焦点在输入框或按着修饰键时方向键不处理 |
| 按住图片右下角的按钮 | 同时对比 n 组时每张图上有 n−1 个按钮，按住时这张图换成对应结果集当前的图片，松开、移出或失焦后恢复；键盘按住空格或回车同样生效 |
| Mod + 滚轮 | 以鼠标位置为中心缩放，所有图片同步，范围 100%–1600%；Mac 触控板双指捏合同样生效 |
| Mod + 左键拖动 | 所有图片同步平移，平移范围限制在图片仍铺满显示区域之内 |
| − / + / 适应窗口 | 以中心缩放，或恢复 100% |

点结果名（网格表头或缩略图列头）可把它设为对齐基准，最近一次点击缩略图的结果也会成为基准。其他结果当前图片文件名中最长的一段数字与基准不同时，表头显示“编号不一致”，用于发现 Mod+单击 按序号同步时因缺图造成的错位。

几点实现说明：

- 浏览器默认把 Ctrl+滚轮当作页面缩放，React 的 `onWheel` 是 passive 监听无法阻止，所以在网格容器上挂了 `passive: false` 的原生 `wheel` 监听。
- 所有图片共用一组缩放和平移，图片显示区域固定 4:3，图片按 `object-fit: contain` 放在其中；放大后用 `image-rendering: pixelated`，便于看像素级差异。
- 缩略图直接加载原图（`loading="lazy"`），结果集很大时滚动缩略图会产生较多请求；后端以后提供缩略图接口时可替换 `CompareImages.tsx` 里的 `url`。

## 后端改动

前端只在 `server/app/main.py` 增加了 `mount_web(...)` 调用，新增 `server/app/web.py`，`config.py` 增加 `web_dir` 字段，没有改动任何接口。
