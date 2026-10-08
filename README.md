# GPU/NPU 服务器管理平台

统一管理昇腾 NPU 和英伟达 GPU 服务器：查看服务器与卡状态、按属性分组筛选、任务排队调度、推理结果评测与对比。

设计文档：<https://claude.ai/code/artifact/249a2da0-b3ac-4760-833d-44408f4e2aec>

## 进度

| 里程碑 | 状态 |
| --- | --- |
| 1. 服务器与卡状态（Agent 采集、属性管理、分组筛选） | 已完成 |
| 2. 任务调度（排队、分配卡、取消、重排、日志） | 已完成 |
| 3. 推理结果与评测 | 未开始 |
| 4. 前端对接 | 进行中（页面已完成，结果与评测按 PR #3 对接，合并前显示示例数据） |

## 目录

```
agent/          节点 Agent（单文件，仅依赖 Python 3.7+ 标准库）
server/app/     中心服务（FastAPI + SQLAlchemy）
web/            前端（Vite + React），说明见 docs/frontend.md
docs/           接口说明 api.md、openapi.json 与前端说明 frontend.md
tests/          测试与 smi 输出样例
scripts/        辅助脚本
```

## 部署 Agent（每台服务器）

把 `agent/` 目录复制到服务器上，以 root 执行：

```sh
sudo ./install.sh <运行用户> <token> [端口，默认 9100]
```

Agent 会自动检测 `nvidia-smi` 或 `npu-smi`。任务以安装时指定的运行用户执行，任务记录和日志保存在该用户的 `~/.gnm-agent/jobs/`（可用 `GNM_AGENT_DATA_DIR` 修改）。命令不在 PATH 中时，可在 `/etc/gnm-agent.env` 里用 `GNM_NVIDIA_SMI` / `GNM_NPU_SMI` 指定路径。

> `npu-smi info` 的解析目前基于公开资料中的 910B 和 310P 输出样例，接入真实机器后需要核对一次。

## 运行中心服务

```sh
pip install -r server/requirements.txt
cd server
GNM_AGENT_TOKEN=<token> uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

先构建前端（需要 Node 18+，构建产物可以拷到没有 Node 的机器上）：

```sh
cd web && npm ci && npm run build
```

打开 `http://<地址>:8000/` 进入管理页面，`http://<地址>:8000/docs` 可在线调试接口。常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GNM_DATABASE_URL` | `sqlite:///./data/gnm.db` | 数据库地址，可换成 PostgreSQL |
| `GNM_AGENT_TOKEN` | 空 | 与 Agent 共享的令牌 |
| `GNM_POLL_INTERVAL` | `10` | 状态轮询间隔（秒） |
| `GNM_OFFLINE_AFTER` | `3` | 连续失败多少次判定离线 |
| `GNM_HISTORY_DAYS` | `7` | 趋势数据保留天数 |
| `GNM_IDLE_MEMORY_MB_GPU` | `1024` | GPU 空闲判定的显存阈值（MB） |
| `GNM_IDLE_MEMORY_MB_NPU` | `6144` | NPU 空闲判定的 HBM 阈值（MB），空载时也有 3~4 GB 占用 |
| `GNM_SCHEDULE_INTERVAL` | `5` | 调度器扫描队列的间隔（秒） |
| `GNM_SCHEDULE_STRICT` | `0` | 设为 `1` 时严格按队列顺序调度，前面的任务放不下时后面的也不调度 |
| `GNM_WEB_DIR` | `web/dist` | 前端构建产物目录，不存在时只提供接口 |

## 开发

```sh
pip install -r requirements-dev.txt
pytest
python scripts/export_openapi.py   # 接口变更后更新 docs/openapi.json
```
