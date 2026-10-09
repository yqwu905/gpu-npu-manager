// 后端缺少推理结果与评测接口时使用的示例数据，内容与前端设计稿一致，结构与后端接口一致
import type {
  CompareMetrics, CompareSampleItem, CompareSamples, CompareSort, EvalConfig, EvalConfigBody, Evaluation, EvaluationCreate, Evaluator, Project,
  ResultFilters, ResultSet, ResultSetCreate, ResultSetUpdate, Sample, SamplePage, TagCount,
} from '../api/types'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

type Kind = 'image' | 'text'
const kindOf = new Map<number, Kind>()

function rs(
  id: number, kind: Kind, name: string, model: string, dataset: string, server: [number, string], path: string,
  sample_count: number, job_id: number | null, metrics: Record<string, number>, evaluating: boolean, params: string,
): ResultSet {
  kindOf.set(id, kind)
  const project = projectList.find((p) => p.id === (kind === 'image' ? 1 : 2))!
  return {
    id, name, server_id: server[0], server_name: server[1], path, sample_count, job_id, metrics, evaluating, note: null,
    project_id: project.id, project_name: project.name, tags: id % 2 ? ['v1'] : ['v2', '发布候选'], lq_metrics: kind === 'image' && Object.keys(metrics).length ? { psnr: 24.9, ssim: 0.702 } : {},
    meta: { model, dataset, params }, created_at: ago(id * 30),
  }
}

let projectList: Project[] = [
  { id: 1, name: '超分辨率', description: 'DIV2K 等超分实验', result_count: 0, created_at: ago(9000) },
  { id: 2, name: '票据 OCR', description: null, result_count: 0, created_at: ago(8000) },
]

let configs: EvalConfig[] = [
  {
    id: 1, name: 'DIV2K ×4 标准评测', metrics: ['psnr', 'ssim', 'lpips'], label_file: null, gt_dir: '/data/datasets/DIV2K/valid_HR',
    lq_dir: '/data/datasets/DIV2K/valid_LR_x4', server_id: 4, server_name: 'gpu-l40s-01', server_path: '/data/eval', num_devices: 1, python: '/data/envs/eval/bin/python', note: null,
    created_at: ago(5000), updated_at: ago(5000),
  },
  {
    id: 2, name: '票据 v2 OCR', metrics: ['ocr_a', 'cer', 'ned'], label_file: '/data/datasets/bills-v2/Label.txt', gt_dir: null, lq_dir: null,
    server_id: null, server_name: null, server_path: null, num_devices: 1, python: null, note: '在结果所在服务器上评测', created_at: ago(4000), updated_at: ago(4000),
  },
]

let sets: ResultSet[] = [
  rs(1, 'image', 'sr-x4-baseline', 'EDSR-baseline', 'DIV2K-val ×4', [5, 'gpu-l40s-02'], '/data/results/sr-x4-baseline', 100, 1270, { psnr: 28.41, ssim: 0.812, lpips: 0.214 }, false, 'scale=4, tile=512'),
  rs(2, 'image', 'sr-x4-v2-perceptual', 'ESRGAN-v2', 'DIV2K-val ×4', [6, 'gpu-4090-01'], '/data/results/sr-x4-v2', 100, 1276, { psnr: 27.96, ssim: 0.805, lpips: 0.162 }, false, 'scale=4, tile=512, fp16'),
  rs(3, 'image', 'sr-x4-v3-distill-npu', 'SR-v3-distill', 'DIV2K-val ×4', [10, 'npu-910b-03'], '/data/results/sr-x4-v3-distill', 100, 1284, { psnr: 28.73, ssim: 0.826, lpips: 0.171 }, false, 'scale=4, tile=256, bf16'),
  rs(4, 'image', 'deblur-v1', 'Deblur-v1', 'GoPro-test', [6, 'gpu-4090-01'], '/data/results/deblur-v1', 1111, 1278, {}, true, 'patch=256'),
  rs(5, 'image', 'sr-x4-v4-preview', 'SR-v4', 'DIV2K-val ×4', [4, 'gpu-l40s-01'], '/data/results/sr-x4-v4-preview', 20, null, {}, false, 'scale=4'),
  rs(6, 'text', 'ocr-ppocrv4-test', 'PP-OCRv4', '票据测试集 v2', [11, 'npu-910b-04'], '/data/results/ocr-ppocrv4', 2000, null, { ocr_a: 0.912, cer: 0.041, ned: 0.953 }, false, 'det_db_thresh=0.3'),
  rs(7, 'text', 'ocr-qwen-vl-ft', 'Qwen2-VL-ft', '票据测试集 v2', [10, 'npu-910b-03'], '/data/results/ocr-qwen-vl-ft', 2000, 1263, { ocr_a: 0.947, cer: 0.026, ned: 0.971 }, false, 'max_new_tokens=256'),
  rs(8, 'text', 'ocr-910b-int8', 'PP-OCRv4-int8', '票据测试集 v2', [13, 'npu-310p-01'], '/data/results/ocr-910b-int8', 2000, 1281, { ocr_a: 0.938, cer: 0.031, ned: 0.965 }, false, 'int8, batch=16'),
]

const EVALUATORS: Evaluator[] = [
  { name: 'psnr', label: 'PSNR', kind: 'image', unit: 'dB', higher_is_better: true, description: '峰值信噪比，越高越好' },
  { name: 'ssim', label: 'SSIM', kind: 'image', unit: null, higher_is_better: true, description: '结构相似度，越高越好' },
  { name: 'lpips', label: 'LPIPS', kind: 'image', unit: null, higher_is_better: false, description: '感知距离，越低越好；可用 1 张卡加速' },
  { name: 'ocr_a', label: 'OCR-A', kind: 'text', unit: null, higher_is_better: true, description: '与参考文本完全一致的样本比例' },
  { name: 'cer', label: 'CER', kind: 'text', unit: null, higher_is_better: false, description: '字符错误率，越低越好' },
  { name: 'ned', label: '1-NED', kind: 'text', unit: null, higher_is_better: true, description: '1 - 归一化编辑距离，越高越好' },
]

function ev(id: number, setId: number, metrics: string[], job_id: number, status: Evaluation['status'], values: Record<string, number | null> | null, error: string | null, min: number): Evaluation {
  const s = findSet(setId)
  return {
    id, result_set_id: setId, result_set_name: s.name, metrics, reference: null, config_id: null, config_name: null, label_file: null, gt_dir: null, lq_dir: null, python: null,
    server_id: s.server_id, server_name: s.server_name, data_path: s.path, output_dir: `${s.path}/eval/${id}`, compute_lq: false, lq_source_id: null,
    lq_values: null, lq_counts: null, lq_errors: null, job_id, job_status: status === 'pending' ? 'queued' : status,
    status, values, counts: null, errors: null, num_skipped: values ? 0 : null, error, created_at: ago(min), finished_at: status === 'succeeded' || status === 'failed' ? ago(min - 3) : null,
  }
}

let evaluations: Evaluation[] = [
  ev(4, 4, ['psnr', 'ssim', 'lpips'], 1288, 'running', null, null, 31),
  ev(3, 3, ['psnr', 'ssim', 'lpips'], 1277, 'succeeded', { psnr: 28.73, ssim: 0.826, lpips: 0.171 }, null, 6),
  ev(2, 8, ['ocr_a', 'cer', 'ned'], 1280, 'succeeded', { ocr_a: 0.938, cer: 0.031, ned: 0.965 }, null, 58),
  ev(1, 2, ['psnr', 'ssim', 'lpips'], 1274, 'failed', null, '参考图读取失败：/data/datasets/DIV2K/val_HR/0001.png 不存在', 1300),
]

// 逐样本示例：图像 PSNR、文字识别结果
const IMG_PSNR: Record<string, number[]> = {
  '0007': [27.1, 25.3, 28.0], '0013': [29.4, 29.0, 29.6], '0021': [24.8, 26.9, 25.2],
  '0034': [31.2, 30.1, 31.5], '0042': [26.0, 23.4, 26.3], '0058': [28.8, 28.5, 28.9],
}
const TEXTS: [string, string, string[]][] = [
  ['0002', '合计金额 ¥1,286.50', ['合计金额 ¥1,236.50', '合计金额 ¥1,286.50', '合计全额 ¥1,286.50']],
  ['0011', '增值税专用发票', ['增值税专用发栗', '增值税专用发票', '增值税专甲发票']],
  ['0017', '开票日期：2024年03月15日', ['开票日期：2024年03月15日', '开栗日期：2024年03月15日', '开票日期：2024年08月15日']],
  ['0023', '税率 13%', ['税率 13%', '税率 13%', '税率 13%']],
  ['0031', '销售方：某某贸易有限公司', ['销售方：某某贸易有限公可', '销售方：某某贸易有限公司', '销售方：某某贸易有限公可']],
]

function editDistance(a: string[], b: string[]): number {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)])
  for (let j = 1; j <= b.length; j++) d[0][j] = j
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
  return d[a.length][b.length]
}

function textMetrics(pred: string, ref: string): Record<string, number> {
  const a = Array.from(pred), b = Array.from(ref)
  const dist = editDistance(a, b)
  return { ocr_a: dist === 0 ? 1 : 0, cer: +(dist / b.length).toFixed(3), ned: +(1 - dist / Math.max(a.length, b.length, 1)).toFixed(3) }
}

// 示例数据只有 PSNR 一组逐样本值，SSIM、LPIPS 由它换算，仅用于演示排序和展示
const imageMetrics = (p: number): Record<string, number> => ({ psnr: p, ssim: +(0.7 + (p - 23) / 40).toFixed(3), lpips: +(0.35 - (p - 23) / 50).toFixed(3) })

function findSet(id: number): ResultSet {
  const s = sets.find((x) => x.id === id)
  if (!s) throw new Error('结果集不存在')
  return s
}

const evaluated = (s: ResultSet) => Object.keys(s.metrics).length > 0

function sampleOf(s: ResultSet, sid: string, i: number): Sample {
  if (kindOf.get(s.id) === 'image') {
    const vals = IMG_PSNR[sid]
    const p = vals ? vals[(s.id - 1) % 3] : (s.metrics.psnr ?? 28) - 2 + ((Number(sid) * 7) % 10) / 2.5
    return {
      id: sid, image: `images/${sid}.png`, ref_image: `/data/datasets/DIV2K/valid_HR/${sid}.png`, metrics: evaluated(s) ? imageMetrics(+p.toFixed(2)) : {},
      ...(evaluated(s) ? { lq_image: `/data/datasets/DIV2K/valid_LR_x4/${sid}.png`, media: { lq_image: 3 } } : {}),
    }
  }
  const t = TEXTS.find((x) => x[0] === sid) ?? TEXTS[i % TEXTS.length]
  const pred = t[2][(s.id - 6) % 3] ?? t[1]
  return { id: sid, image: `crops/${sid}.png`, text: pred, ref_text: t[1], metrics: evaluated(s) ? textMetrics(pred, t[1]) : {} }
}

const withCounts = () => projectList.map((p) => ({ ...p, result_count: sets.filter((s) => s.project_id === p.id).length }))

export const mockResults = {
  list: (f: ResultFilters = {}) => sets.filter((s) =>
    (f.project_id === undefined || (s.project_id ?? 0) === f.project_id) &&
    (f.tag ?? []).every((t) => s.tags.includes(t)) &&
    (!f.q || `${s.name} ${s.path} ${s.note ?? ''}`.toLowerCase().includes(f.q.toLowerCase()))),
  update(id: number, body: ResultSetUpdate): ResultSet {
    sets = sets.map((s) => (s.id === id ? { ...s, ...body, project_name: projectList.find((p) => p.id === ('project_id' in body ? body.project_id : s.project_id))?.name ?? null } : s))
    return findSet(id)
  },
  tags(): TagCount[] {
    const counts = new Map<string, number>()
    sets.forEach((s) => s.tags.forEach((t) => counts.set(t, (counts.get(t) ?? 0) + 1)))
    return [...counts].map(([tag, count]) => ({ tag, count }))
  },
  projects: withCounts,
  createProject(body: { name: string; description?: string | null }): Project {
    const p = { id: Math.max(0, ...projectList.map((x) => x.id)) + 1, name: body.name, description: body.description ?? null, result_count: 0, created_at: ago(0) }
    projectList = [...projectList, p]
    return p
  },
  updateProject(id: number, body: { name?: string; description?: string | null }): Project {
    projectList = projectList.map((p) => (p.id === id ? { ...p, ...body } : p))
    return withCounts().find((p) => p.id === id)!
  },
  deleteProject(id: number) {
    projectList = projectList.filter((p) => p.id !== id)
    sets = sets.map((s) => (s.project_id === id ? { ...s, project_id: null, project_name: null } : s))
  },
  configs: () => configs,
  saveConfig(id: number | null, body: Partial<EvalConfigBody>): EvalConfig {
    if (id === null) {
      const c = { ...(body as EvalConfigBody), id: Math.max(0, ...configs.map((x) => x.id)) + 1, server_name: body.server_id ? `服务器 ${body.server_id}` : null, created_at: ago(0), updated_at: ago(0) }
      configs = [...configs, c]
      return c
    }
    configs = configs.map((c) => (c.id === id ? { ...c, ...body, updated_at: ago(0) } : c))
    return configs.find((c) => c.id === id)!
  },
  deleteConfig(id: number) {
    configs = configs.filter((c) => c.id !== id)
  },
  create(body: ResultSetCreate): ResultSet {
    const name = body.name?.trim() || body.path.split('/').filter(Boolean).pop() || 'result'
    const s = rs(sets.length + 1, 'image', name, '-', '-', [body.server_id, `服务器 ${body.server_id}`], body.path, 0, body.job_id ?? null, {}, false, '')
    sets = [s, ...sets]
    return s
  },
  samples(id: number, offset: number, limit: number): SamplePage {
    const s = findSet(id)
    const ids = Array.from({ length: s.sample_count ?? 0 }, (_, i) => String(i).padStart(4, '0'))
    return { total: ids.length, offset, items: ids.slice(offset, offset + limit).map((sid, i) => sampleOf(s, sid, offset + i)) }
  },
  evaluators: () => EVALUATORS,
  evaluations: (resultSetId?: number) => evaluations.filter((e) => resultSetId === undefined || e.result_set_id === resultSetId),
  evaluate(body: EvaluationCreate): Evaluation {
    const config = configs.find((c) => c.id === body.config_id)
    const e = ev(Math.max(...evaluations.map((x) => x.id)) + 1, body.result_set_id, body.metrics ?? config?.metrics ?? [], 1299, config?.server_id ? 'copying' : 'pending', null, null, 0)
    if (config) Object.assign(e, { config_id: config.id, config_name: config.name, lq_dir: config.lq_dir, gt_dir: config.gt_dir, label_file: config.label_file, python: config.python })
    if (body.python !== undefined) e.python = body.python
    e.reference = body.reference ?? null
    evaluations = [e, ...evaluations]
    sets = sets.map((s) => (s.id === body.result_set_id ? { ...s, evaluating: true } : s))
    return e
  },
  compareMetrics(ids: number[]): CompareMetrics {
    const chosen = ids.map(findSet)
    const present = new Set(chosen.flatMap((s) => Object.keys(s.metrics)))
    return {
      result_sets: chosen.map((s) => ({ id: s.id, name: s.name, server_name: s.server_name, meta: s.meta })),
      metrics: EVALUATORS.filter((m) => present.has(m.name)),
      values: Object.fromEntries(chosen.map((s) => [String(s.id), { ...s.metrics }])),
      lq_values: Object.fromEntries(chosen.filter((s) => Object.keys(s.lq_metrics).length).map((s) => [String(s.id), { ...s.lq_metrics }])),
    }
  },
  compareSamples(ids: number[], metric: string | undefined, sort: CompareSort, offset: number, limit: number): CompareSamples {
    const chosen = ids.map(findSet)
    const order = kindOf.get(chosen[0].id) === 'text' ? TEXTS.map((t) => t[0]) : Object.keys(IMG_PSNR)
    let items: CompareSampleItem[] = order.map((sid, i) => ({
      id: sid,
      results: Object.fromEntries(chosen.map((s) => [String(s.id), kindOf.get(s.id) === kindOf.get(chosen[0].id) ? sampleOf(s, sid, i) : null])),
      spread: null,
    }))
    if (metric) {
      items = items.map((it) => {
        const vs = Object.values(it.results).map((r) => r?.metrics[metric]).filter((v): v is number => typeof v === 'number')
        return { ...it, spread: vs.length >= 2 ? +(Math.max(...vs) - Math.min(...vs)).toFixed(3) : null }
      })
      const first = (it: CompareSampleItem) => it.results[String(chosen[0].id)]?.metrics[metric] ?? 0
      if (sort === 'spread') items.sort((a, b) => (b.spread ?? -1) - (a.spread ?? -1))
      else items.sort((a, b) => (sort === 'asc' ? first(a) - first(b) : first(b) - first(a)))
    }
    return { total: items.length, offset, items: items.slice(offset, offset + limit) }
  },
}
