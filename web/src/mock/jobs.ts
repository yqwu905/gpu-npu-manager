// 第 2 步任务接口上线前使用的示例数据，内容与前端设计稿一致；修改只保存在当前页面内存中
import type { Job, JobCreate, JobLog, JobPage, JobQuery, SchedulerSettings } from '../api/types'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

let nextId = 1299
let scheduler: SchedulerSettings = { strict_order: false }

function job(
  id: number, name: string, submitter: string, status: Job['status'], num_devices: number, group: string,
  server: [number, string] | null, devices: number[], priority: number, createdMin: number, startedMin: number | null,
  finishedMin: number | null, extra: Partial<Job> = {},
): Job {
  const started = startedMin === null ? null : ago(startedMin)
  return {
    id, name, submitter, status, num_devices, group, priority, accelerator: null, queue_position: null, error: null, requeued_from: null,
    command: num_devices === 0 ? `python eval/run.py --result /data/results/${name}` : `torchrun --nproc_per_node=${num_devices} train.py --config configs/${name}.yaml`,
    workdir: `/data/projects/${name.split(' ')[0]}`,
    env: { HF_HOME: '/data/hf', WANDB_MODE: 'offline' },
    server_id: null,
    assigned_server_id: server ? server[0] : null,
    assigned_server_name: server ? server[1] : null,
    device_indices: devices,
    pid: started ? 31000 + id : null,
    exit_code: status === 'succeeded' ? 0 : status === 'failed' ? 137 : status === 'cancelled' && started ? -15 : null,
    wait_reason: status === 'queued' ? '下一轮调度' : null,
    created_at: ago(createdMin),
    started_at: started,
    finished_at: finishedMin === null ? null : ago(finishedMin),
    ...extra,
  }
}

const range = (a: number, b: number) => Array.from({ length: b - a + 1 }, (_, i) => a + i)

let jobs: Job[] = [
  job(1293, 'sd-ft-lora-v3', '张三', 'queued', 4, '训练组', null, [], 5, 12, null, null, { wait_reason: '下一轮调度：gpu-a100-02 有 4 张空闲卡' }),
  job(1295, 'llm-sft-7b-zh', '李四', 'queued', 8, '训练组', null, [], 0, 72, null, null, { wait_reason: '训练组没有一台服务器有 8 张空闲卡（npu-910b-05 空闲 8 张但不参与调度）' }),
  job(1296, 'ocr-eval-batch7', '孙八', 'queued', 0, '评测组', null, [], 0, 3, null, null),
  job(1297, 'sr-x4-v4-infer', '王五', 'queued', 2, '推理组', null, [], 0, 1, null, null),
  job(1298, 'sr-x4-v4-eval', '王五', 'queued', 0, '评测组', null, [], -1, 0, null, null, { wait_reason: '回填中：优先级较低，等前面的任务调度后再尝试' }),
  job(1285, 'llm-pretrain-1.3b', '钱七', 'running', 8, '训练组', [8, 'npu-910b-01'], range(0, 7), 0, 291, 290, null),
  job(1286, 'ocr-qwen-vl-ft-v2 推理', '孙八', 'running', 2, '推理组', [10, 'npu-910b-03'], [0, 1], 0, 143, 142, null),
  job(1287, 'llama-sft-13b', '张三', 'running', 8, '训练组', [1, 'gpu-a100-01'], range(0, 7), 3, 238, 237, null),
  job(1288, 'deblur-v1 评测', '赵六', 'running', 2, '评测组', [6, 'gpu-4090-01'], [0, 1], 0, 32, 31, null),
  job(1289, 'llm-sft-7b-zh-dpo', '李四', 'running', 6, '训练组', [3, 'gpu-h800-01'], range(0, 5), 0, 103, 102, null),
  job(1290, 'sd-ft-lora-v2', '张三', 'running', 4, '训练组', [2, 'gpu-a100-02'], range(0, 3), 0, 75, 74, null),
  job(1291, 'ocr-det-train', '钱七', 'running', 4, '训练组', [9, 'npu-910b-02'], range(0, 3), 0, 61, 60, null),
  job(1292, 'sr-x4-v4-train', '王五', 'running', 1, '推理组', [4, 'gpu-l40s-01'], [0], 0, 48, 47, null),
  job(1294, 'ocr-910b-int8-bench', '赵六', 'running', 1, '评测组', [13, 'npu-310p-01'], [0], 0, 19, 18, null),
  job(1284, 'sr-x4-v3-distill-npu 推理', '王五', 'succeeded', 2, '推理组', [10, 'npu-910b-03'], [2, 3], 0, 47, 46, 8),
  job(1283, 'llm-sft-7b-zh 试跑', '李四', 'failed', 8, '训练组', [3, 'gpu-h800-01'], range(0, 7), 0, 51, 50, 26, { error: '进程退出码 137（可能被 OOM 终止）' }),
  job(1282, 'ocr-qwen-vl-ft 推理', '孙八', 'cancelled', 2, '推理组', [11, 'npu-910b-04'], [2, 3], 0, 51, 50, 41),
  job(1281, 'ocr-910b-int8 推理', '赵六', 'succeeded', 1, '评测组', [13, 'npu-310p-01'], [0], 0, 83, 82, 60),
  job(1279, 'sr-x4-v2 推理', '王五', 'lost', 4, '评测组', [7, 'gpu-4090-02'], range(0, 3), 0, 200, 199, null),
]

function find(id: number): Job {
  const j = jobs.find((x) => x.id === id)
  if (!j) throw new Error('任务不存在')
  return j
}

function patch(id: number, p: Partial<Job>): Job {
  jobs = jobs.map((j) => (j.id === id ? { ...j, ...p } : j))
  return find(id)
}

function logText(j: Job): string {
  if (!j.started_at) return ''
  const env = j.assigned_server_name?.startsWith('gpu') ? 'CUDA_VISIBLE_DEVICES' : 'ASCEND_RT_VISIBLE_DEVICES'
  const lines = [`使用 ${env}=${j.device_indices.join(',')}`, `world_size=${j.num_devices} 初始化完成`]
  for (let s = 100; s <= 800; s += 100) lines.push(`epoch 1/3 step ${s} loss=${(1.9 - s / 1000).toFixed(3)} lr=2.0e-5`)
  if (j.status === 'failed') lines.push('RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB', 'Killed (exit 137)')
  if (j.status === 'succeeded') lines.push('完成，退出码 0')
  return lines.map((l) => `[示例] ${l}\n`).join('')
}

export const mockJobs = {
  list(q: JobQuery): JobPage {
    const items = jobs
      .filter((j) => (!q.status?.length || q.status.includes(j.status)) && (!q.submitter || j.submitter === q.submitter) && (!q.group || j.group === q.group))
      .sort((a, b) => b.id - a.id)
    const queued = jobs.filter((j) => j.status === 'queued').sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at))
    return { total: items.length, items: items.map((j) => ({ ...j, queue_position: j.status === 'queued' ? queued.indexOf(j) + 1 : null })) }
  },
  get: (id: number) => find(id),
  create(body: JobCreate): Job {
    const j = job(nextId++, body.name || body.command.split('\n')[0].slice(0, 60), body.submitter || '', 'queued', body.num_devices, body.group ?? '', null, [], body.priority ?? 0, 0, null, null, {
      command: body.command, workdir: body.workdir ?? null, env: body.env ?? {}, server_id: body.server_id ?? null,
    })
    jobs = [...jobs, j]
    return j
  },
  cancel(id: number): Job {
    const j = find(id)
    return patch(id, { status: 'cancelled', finished_at: new Date().toISOString(), exit_code: j.started_at ? -15 : null, wait_reason: null })
  },
  requeue(id: number): Job {
    const j = find(id)
    const n: Job = { ...j, id: nextId++, status: 'queued', assigned_server_id: null, assigned_server_name: null, device_indices: [], pid: null, exit_code: null,
      error: null, requeued_from: j.id, wait_reason: '下一轮调度', created_at: new Date().toISOString(), started_at: null, finished_at: null }
    jobs = [...jobs, n]
    return n
  },
  setPriority: (id: number, priority: number) => patch(id, { priority }),
  log(id: number, offset: number): JobLog {
    const text = logText(find(id))
    return { offset, next_offset: text.length, size: text.length, data: text.slice(offset) }
  },
  scheduler: () => scheduler,
  setScheduler(s: SchedulerSettings) {
    scheduler = s
    return scheduler
  },
}
