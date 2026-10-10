# GPU/NPU 服务器管理平台

统一管理昇腾 NPU 和英伟达 GPU 服务器：查看服务器与卡状态、按属性分组筛选、任务排队调度、推理结果评测与对比。

设计文档：<https://claude.ai/code/artifact/249a2da0-b3ac-4760-833d-44408f4e2aec>

## 进度

| 里程碑 | 状态 |
| --- | --- |
| 1. 服务器与卡状态（Agent 采集、属性管理、分组筛选） | 已完成 |
| 2. 任务调度（排队、分配卡、取消、重排、日志） | 已完成 |
| 3. 推理结果与评测（结果浏览、评测、多组对比） | 已完成 |
| 4. 前端对接 | 已完成 |

## 目录

```
agent/          节点 Agent（agent.py、evaluate.py、imaging.py，仅依赖 Python 3.7+ 标准库，Pillow 可选）
server/app/     中心服务（FastAPI + SQLAlchemy）
web/            前端（Vite + React），说明见 docs/frontend.md
docs/           接口说明 api.md、openapi.json 与前端说明 frontend.md
tests/          测试与 smi 输出样例
scripts/        辅助脚本
```

## 安装中心服务（手动，一次）

需要 Python 3.10+ 和 OpenSSH 客户端；构建前端需要 Node 18+（也可以在别的机器上构建好再拷贝 `web/dist`）。

```sh
git clone https://github.com/yqwu905/gpu-npu-manager && cd gpu-npu-manager
(cd web && npm ci && npm run build)
pip install -r server/requirements.txt
cd server
GNM_AGENT_TOKEN=<token> uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

打开 `http://<地址>:8000/` 进入管理页面，`http://<地址>:8000/docs` 可在线调试接口。升级中心服务时 `git pull` 后重新构建前端并重启即可，数据库会自动补上新增的列。`server/requirements.txt` 里有 Pillow：节点没有 Pillow 时，对比页的缩略图和预览由中心服务生成。多人使用对比页时建议在前面加一层支持 HTTP/2 的反向代理，见下文“部署建议”。

### 部署建议

对比页会发出大量缩略图、预览和瓦片请求。浏览器对同一地址最多建 6 个 HTTP/1.1 连接，前端据此把图片请求限制在同时 5 个（缩略图最多 3 个、预取最多 2 个，留一个给其他接口）；页面经 HTTP/2 或 HTTP/3 访问时放宽到 16 个（缩略图 8 个、预取 6 个）。uvicorn 只提供 HTTP/1.1，所以多人使用或同时对比的结果集多时，建议在中心服务前面放一个支持 HTTP/2 的反向代理，如 nginx 或 Caddy。浏览器只在 HTTPS 上使用 HTTP/2，内网可以用自签或内部 CA 的证书。

原图（`/file`、`/image?kind=full`）由中心服务流式转发，代理不要缓冲，否则大文件要等代理收完才开始发给浏览器。nginx 示例：

```nginx
upstream gnm { server 127.0.0.1:8000; keepalive 32; }
server {
    listen 443 ssl;
    http2 on;                      # nginx 1.25.1 之前写成 listen 443 ssl http2;
    ssl_certificate     /etc/nginx/gnm.crt;
    ssl_certificate_key /etc/nginx/gnm.key;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_read_timeout 180s;       # 不小于 GNM_IMAGE_LIST_TIMEOUT（120 秒）
    location / { proxy_pass http://gnm; }
    # location 匹配不到查询参数，所以 /image 整体不缓冲；缩略图等派生图都很小，影响不大
    location ~ ^/api/results/\d+/(file|image)$ {
        proxy_pass http://gnm;
        proxy_buffering off;
    }
}
```

Caddy 自动启用 HTTPS、HTTP/2 和 HTTP/3，默认不缓冲响应，一行即可（内网地址可在站点块里加 `tls internal`，用 Caddy 自带的 CA）：

```
gnm.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

另外：

- 节点上装 Pillow。派生图在节点生成时只传几十到几百 KB 的结果；没有 Pillow 时每张原图都要先传到中心服务。
- 节点的家目录在 NFS 上时，把 `GNM_AGENT_CACHE_DIR` 指到本地盘。
- 8K 大图多、节点 CPU 多时调大 `GNM_AGENT_IMAGE_WORKERS`：实测 4 核环境下 6 组同时冷切到 8K 图，2 个解码线程要 3.5 秒，4 个为 2.5 秒。
- 实测数据见 [docs/frontend.md](docs/frontend.md) 的“性能实测”。

## 添加服务器（自动安装 Agent）

Agent 由中心服务通过 SSH 安装和升级，不需要登录各服务器操作：

1. 在中心主机上准备 SSH 密钥（`ssh-keygen`），用 `ssh-copy-id <用户>@<服务器>` 把公钥分发到各服务器；页面“添加服务器”对话框里也会显示中心主机的公钥。
2. 在“服务器”页点“添加服务器”，单台填写地址和 SSH 用户；或切到“批量”，每行一台：`[用户@]地址[:SSH端口] [名称]`；或切到“从 SSH 配置导入”，勾选中心主机 `~/.ssh/config` 里的 Host。导入的服务器以 Host 别名作为名称和 SSH 连接目标，沿用 config 中的密钥、跳板机等设置，Agent 地址取 HostName。
3. 中心服务登录后把 `agent.py`、`evaluate.py`、`imaging.py` 写到该用户的 `~/.gnm-agent/bin/`，启动 Agent 并用 crontab `@reboot` 设置开机自启。不需要 root，任务以该 SSH 用户运行。安装进度、错误和完整输出在服务器详情的“Agent”页。
4. 中心服务默认通过 SSH 端口转发访问 Agent（为每台服务器维持一个 `ssh -N -L` 进程，断开后自动重建），Agent 只监听服务器本机，防火墙不需要放通 Agent 端口。如果服务器的 sshd 禁止端口转发（`AllowTcpForwarding no`，openEuler 等系统默认如此），会自动改为经 SSH 会话中继（由服务器上的 `python3` 连接 Agent，并复用 SSH 连接），不需要改 sshd 配置。个别服务器可以在“属性”里关闭转发，改为直接连接 Agent 端口（关闭后需要重新安装 Agent）。
5. 中心服务升级后，版本落后的托管 Agent 会自动升级一次（`GNM_AUTO_UPGRADE=0` 可关闭）；也可以在页面上逐台或一键升级。升级只重启 Agent 进程，运行中的任务不受影响。

服务器需要有 `python3`（3.7+）、`bash` 和 `base64`；关闭 SSH 转发或手动安装 Agent 时，中心主机要能直接访问 Agent 端口（默认 9100）。命令不在 PATH 中时，可在服务器的 `~/.gnm-agent/agent.local.env` 里写 `export GNM_NPU_SMI=/path/to/npu-smi`（升级不会覆盖），然后在页面上重新安装。

Agent 会自动检测 `nvidia-smi` 或 `npu-smi`，任务记录和日志保存在 `~/.gnm-agent/jobs/`。中心服务通过 Agent 读取推理结果，不限制路径，只受 Agent 运行用户（即 SSH 用户）的文件权限约束。

对比页的缩略图、预览和瓦片由 Agent 用 Pillow（7.0 及以上）生成，缓存在 `~/.cache/gnm-agent/img`（默认最多 2 GB）。Pillow 是可选的：没有时 Agent 照常工作，改由中心服务把原图拉过去生成，要多传一次原图，大图明显更慢，安装输出里会给出提示。服务器默认的 `python3` 没有 Pillow、另一个 Python 环境（如 conda、venv，同样要 3.7+）有时，在 `~/.gnm-agent/agent.local.env` 里写 `export GNM_AGENT_PYTHON=/path/to/python3`，重新安装后 Agent 改用它运行。图片相关的其他设置见“配置”。

评测脚本以登录用户的 `python3` 执行，需要 `numpy` 和 `Pillow`；LPIPS 另外需要 `torch` 和 `lpips`（首次运行会下载 AlexNet 权重，离线机器需提前放好缓存）。评测指定了 GPU/NPU 且装了 `torch` 时，PSNR/SSIM 也在卡上计算，否则用 numpy 在 CPU 上算；OCR 要用 GPU 需要装 `paddlepaddle-gpu`。

> `npu-smi info` 的解析目前基于公开资料中的 910B 和 310P 输出样例，接入真实机器后需要核对一次。

### 手动安装 Agent（不用 SSH 时）

把 `agent/` 目录复制到服务器上，以 root 执行 `sudo ./install.sh <运行用户> <token> [端口] [只允许读取的目录]`，装成 systemd 服务；然后在页面上添加服务器时不填 SSH 用户。手动安装的 Agent 不由中心服务升级。`GNM_AGENT_*` 设置写在 `/etc/gnm-agent.env`（每行 `名称=值`，不加 `export`）；要换装了 Pillow 的 Python，改 `/etc/systemd/system/gnm-agent.service` 的 `ExecStart`。改完执行 `systemctl daemon-reload && systemctl restart gnm-agent`。

## 配置

中心服务的环境变量：

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
| `GNM_SCHEDULE_STRICT` | `0` | 设为 `1` 时严格按队列顺序调度，前面的任务放不下时后面的也不调度；运行期可用 `PATCH /api/scheduler` 切换 |
| `GNM_WEB_DIR` | `web/dist` | 前端构建产物目录，不存在时只提供接口 |
| `GNM_EVAL_PYTHON` | `python3` | 运行评测脚本的默认解释器；评测配置或发起评测时填写的 python 优先 |
| `GNM_EVAL_SCRIPT` | 空 | 各服务器上评测脚本的路径，为空时用 Agent 所在目录下的 `evaluate.py` |
| `GNM_SSH_COMMAND` | `ssh` | 安装 Agent 用的 ssh 命令，可附加参数，如 `ssh -i /path/key` |
| `GNM_DEPLOY_CONCURRENCY` | `4` | 同时安装的服务器数 |
| `GNM_DEPLOY_TIMEOUT` | `180` | 单台安装的 SSH 超时（秒） |
| `GNM_COPY_TIMEOUT` | `21600` | 评测前把结果目录拷贝到评测服务器的超时（秒） |
| `GNM_AUTO_UPGRADE` | `1` | 中心服务升级后，自动把版本落后的托管 Agent 升级一次；设为 `0` 关闭，改为在页面上手动升级 |

中心服务的对比页图片设置（接口约定见 [docs/api.md](docs/api.md) 的“对比页的图片接口”）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GNM_IMAGE_CACHE_DIR` | `./data/image-cache` | 派生图缓存和原图暂存（`spool/`）的目录，相对中心服务的工作目录 |
| `GNM_IMAGE_CACHE_MB` | `4096` | 派生图缓存的容量（MB），超出时按最近使用删到 80% |
| `GNM_IMAGE_SPOOL_MB` | `4096` | 原图暂存的容量（MB），节点没有 Pillow 时用；单个文件最大 1 GB |
| `GNM_IMAGE_TIMEOUT` | `60` | 读取图片的超时（秒） |
| `GNM_IMAGE_LIST_TIMEOUT` | `120` | 读取图片列表的超时（秒） |
| `GNM_IMAGE_LIST_CONCURRENCY` | `2` | 每个 Agent 同时读取的图片列表数 |
| `GNM_IMAGE_THUMB_CONCURRENCY` | `6` | 每个 Agent 同时进行的缩略图请求数 |
| `GNM_IMAGE_MAIN_CONCURRENCY` | `6` | 每个 Agent 同时进行的预览和瓦片请求数 |
| `GNM_IMAGE_STREAM_CONCURRENCY` | `4` | 每个 Agent 同时进行的原图和文件流数（含 `/file`） |
| `GNM_IMAGE_QUEUE_WAIT` | `20` | 以上请求排队超过多少秒返回 503 |
| `GNM_IMAGE_WORKERS` | CPU 核数，最多 4 | 中心服务自己生成派生图时同时解码的图片数 |
| `GNM_IMAGE_MEM_MB` | `1024` | 中心服务生成时解码和缩放的内存预算（MB） |
| `GNM_IMAGE_DECODED_MB` | `2048` | 中心服务留在内存里的整图解码结果（MB），同一张图切瓦片不必重复解码 |
| `GNM_IMAGE_MAX_PIXELS` | `3e8` | 单张图片的像素上限，超过时返回 413 |

Agent 的对比页图片设置。SSH 安装的写在服务器的 `~/.gnm-agent/agent.local.env`（`export 名称=值`），在页面上重新安装后生效；手动安装的见上文“手动安装 Agent”：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GNM_AGENT_PYTHON` | 空 | 运行 Agent 的 Python，用来指定装了 Pillow 的环境；只对 SSH 安装的 Agent 有效 |
| `GNM_AGENT_NO_PILLOW` | 空 | 设为 `1` 时不用 Pillow，派生图改由中心服务生成 |
| `GNM_AGENT_CACHE_DIR` | `~/.cache/gnm-agent/img` | 派生图的磁盘缓存目录（设置了 `XDG_CACHE_HOME` 时在它下面） |
| `GNM_AGENT_CACHE_MB` | `2048` | 磁盘缓存容量（MB），超出时按最近使用删到 80%；磁盘剩余不足 1 GB 或 5% 时不再写入 |
| `GNM_AGENT_IMAGE_WORKERS` | CPU 核数 / 4，限制在 2–6 | 同时解码的图片数 |
| `GNM_AGENT_IMAGE_MEM_MB` | 内存 / 16，最多 1024 | 解码和缩放的内存预算（MB），超出时排队 |
| `GNM_AGENT_DECODED_MB` | 内存 / 8，最多 2048 | 留在内存里的整图解码结果（MB），同一张图切瓦片不必重复解码；一张 8K 图约 180 MB |
| `GNM_AGENT_MAX_PIXELS` | `3e8` | 单张图片的像素上限，超过时返回 413 |

## 推理结果格式

一个结果集是服务器上的一个目录：

```
model-a/
  meta.json            可选，描述信息，如 {"name": "model-a", "model": "...", "dataset": "...", "params": {...}}
  predictions.jsonl    可选，每行一个样本
  images/...           图片等文件
  eval/<评测 ID>/      评测输出，由平台生成（metrics.json、per_sample.jsonl）
```

`predictions.jsonl` 是可选的。没有它时，平台扫描目录（跳过 `eval/` 和隐藏文件）：每个图片（png、jpg、jpeg、bmp、webp、tif）或 `.txt` 是一个样本，样本 ID 是去掉扩展名的相对路径，同名的图片和 `.txt` 属于同一个样本，`.txt` 的内容作为识别文本。这时参考值填一个目录，平台按同样的样本 ID 配对其中的图片（`ref_image`）和 `.txt`（`ref_text`），对不上时再按文件名（不含目录）配对。

需要逐样本附加信息时再写 `predictions.jsonl`，每行至少有 `id`，评测用到的字段：

| 字段 | 说明 |
| --- | --- |
| `image` / `ref_image` | 预测图与参考图路径，相对路径相对于 predictions.jsonl 所在目录；用于 PSNR、SSIM、LPIPS |
| `text` / `ref_text` | 识别文本与参考文本；用于 OCR-A、CER、1-NED。没有 `text` 但有 `image` 时，评测会用 PaddleOCR 识别预测图得到文本 |

参考值也可以放在单独的 jsonl 或目录里（发起评测时填 `reference`），jsonl 按 `id` 合并 `ref_image`、`ref_text`。

文字参考值还可以直接用 PaddleOCR 格式的标注文件（如 `Label.txt`），每行是“图片文件名<Tab>文本框 JSON 数组”：

```
xxx_INPUT.jpg	[{"transcription": "手機報在线", "points": [[1179, 551], [1652, 544], [1654, 666], [1181, 673]], "difficult": false}, ...]
```

按图片文件名（去掉扩展名）与样本 ID 配对，对不上时按不含目录的文件名配对。`difficult` 为 true 或内容为 `###` 的框不参与评测；其余框按阅读顺序拼成参考文本：中心高度相近的框算同一行，行内从左到右用空格连接，行与行之间用换行连接。

样本没有现成的识别文本时，评测会在结果所在的服务器上用 PaddleOCR 识别预测图。参考值来自标注文件时，只识别标注框内的文字（标注不一定覆盖图中所有文字）：按每个框的 `points` 裁图（四点框透视变换拉正，竖排的旋转 90 度），只做识别不做检测，逐框与 `transcription` 比较；标注坐标对应参考图（GT），预测图尺寸不同（如 LQ）时按比例缩放。参考文本不是来自标注文件时，对整图做检测和识别，识别出的框按同样的规则拼成文本，再与参考文本比较。识别结果保存在评测输出目录的 `ocr_results.txt`（与标注文件同样的格式）。这需要服务器上运行评测的 Python 环境装有 `paddleocr`（2.x 或 3.x）和对应的 `paddlepaddle`（GPU 用 `paddlepaddle-gpu`，昇腾 NPU 需要 PaddlePaddle 的 NPU 插件）；没有安装时文字指标会显示“OCR 初始化失败”。评测给了卡时 OCR 在卡上运行，0 张卡时用 CPU。3.x 默认的文档方向分类和 UVDoc 去扭曲在评测时关闭。PaddleOCR 第一次运行会从 HuggingFace（或 BOS 等）下载模型到 `~/.paddlex/official_models`，离线服务器需要先把模型放到这个目录。评测日志每 10 秒输出一次进度。其他字段会原样展示在样本浏览页。

### 项目、标签与评测配置

结果集可以归档到项目、打多个标签，列表按项目、标签（需全部包含）和名称关键字筛选。

评测配置把一组评测参数保存下来反复使用：评测哪些指标、文字指标的 Label 文件、GT 目录、LQ 目录，以及评测服务器和其上的路径。这些路径都在评测服务器上；没有指定评测服务器时在结果所在服务器上评测，路径也在那台服务器上。

指定了评测服务器（且与结果所在服务器不同）时，中心服务先通过 SSH 把结果目录（不含 `eval/`）以 tar 流经中心主机拷贝到评测服务器的 `<路径>/result_<结果集 ID>`，再在评测服务器上运行评测，输出写到拷贝目录的 `eval/<评测 ID>/`。两台服务器都要填写 SSH 用户，并且中心主机的公钥已加入它们的 `authorized_keys`。拷贝期间评测状态为“拷贝中”；中心服务重启会中断拷贝，该评测记为失败。

LQ 目录按文件名与样本配对：样本浏览和对比页可以把 LQ 图片与结果并排展示；同时把 LQ 图片当作预测结果，用同样的参考值和指标算一次 LQ 基线指标。同一配置（LQ、GT、Label、评测服务器都不变）只在第一次评测时计算，之后的评测直接复用；修改了这些设置后的第一次评测会重新计算。

指标定义：

| 指标 | 定义 |
| --- | --- |
| PSNR | RGB 8 位图像，`10·log10(255²/MSE)`，两图完全相同时记为 100，各样本平均 |
| SSIM | 11×11 高斯窗、sigma 1.5、K1 0.01、K2 0.03，RGB 三通道平均，与 skimage 的 Wang 2004 设置一致，各样本平均 |
| LPIPS | `lpips` 包的 AlexNet 版本，各样本平均，越低越好 |
| OCR-A | 识别结果与参考完全一致的区域占比。标注文件的每个框是一个区域；按整段文本比较的样本整个算一个区域 |
| CER | 语料级字符错误率：总编辑距离 / 参考文本总字符数，越低越好 |
| 1-NED | 每个区域 `1 - 编辑距离 / max(两者长度)`，各区域平均 |

## 开发

```sh
pip install -r requirements-dev.txt
pytest
python scripts/export_openapi.py   # 接口变更后更新 docs/openapi.json
(cd web && npm run typecheck && npm test)   # 前端类型检查与单元测试
```

对比页的基准测试不随 pytest 运行：`scripts/make_compare_dataset.py` 生成测试数据集，`scripts/bench_stack.py` 在本机启动 Agent 和中心服务并登记结果集，`scripts/bench_images.py` 测后端图片接口，`web/e2e/compare-perf.mjs` 在浏览器里测交互，用法见各脚本开头和 [docs/frontend.md](docs/frontend.md)。
