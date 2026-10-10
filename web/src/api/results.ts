// 带 .ts 后缀：对比页的纯模块会经由本文件被 node --test 直接加载
import { request, requestFull, withFallback } from './client.ts'
import { mockResults } from '../mock/results.ts'
import type {
  CompareMetrics, CompareSamples, CompareSort, EvalConfig, EvalConfigBody, Evaluation, EvaluationCreate, Evaluator, ImageKind, ImageList, Project,
  ResultFilters, ResultSet, ResultSetCreate, ResultSetUpdate, Sample, SamplePage, TagCount,
} from './types'

const fb = <T>(real: () => Promise<T>, mock: () => T | Promise<T>) => withFallback('results', real, mock)
// 预览尺寸向上取到后端的档位，保证同一张图的 URL 一致
const previewBucket = (s: number) => (s <= 1024 ? 1024 : s <= 2048 ? 2048 : 3072)

export const resultsApi = {
  list: (filters: ResultFilters = {}) =>
    fb(() => request<ResultSet[]>('GET', '/results', { query: { ...filters } }), () => mockResults.list(filters)),
  create: (body: ResultSetCreate) => fb(() => request<ResultSet>('POST', '/results', { body }), () => mockResults.create(body)),
  update: (id: number, body: ResultSetUpdate) =>
    fb(() => request<ResultSet>('PATCH', `/results/${id}`, { body }), () => mockResults.update(id, body)),
  tags: () => fb(() => request<TagCount[]>('GET', '/results/tags'), () => mockResults.tags()),
  projects: () => fb(() => request<Project[]>('GET', '/projects'), () => mockResults.projects()),
  createProject: (body: { name: string; description?: string | null }) =>
    fb(() => request<Project>('POST', '/projects', { body }), () => mockResults.createProject(body)),
  updateProject: (id: number, body: { name?: string; description?: string | null }) =>
    fb(() => request<Project>('PATCH', `/projects/${id}`, { body }), () => mockResults.updateProject(id, body)),
  deleteProject: (id: number) => fb(() => request<void>('DELETE', `/projects/${id}`), () => mockResults.deleteProject(id)),
  configs: () => fb(() => request<EvalConfig[]>('GET', '/eval-configs'), () => mockResults.configs()),
  createConfig: (body: EvalConfigBody) => fb(() => request<EvalConfig>('POST', '/eval-configs', { body }), () => mockResults.saveConfig(null, body)),
  updateConfig: (id: number, body: Partial<EvalConfigBody>) =>
    fb(() => request<EvalConfig>('PATCH', `/eval-configs/${id}`, { body }), () => mockResults.saveConfig(id, body)),
  deleteConfig: (id: number) => fb(() => request<void>('DELETE', `/eval-configs/${id}`), () => mockResults.deleteConfig(id)),
  samples: (id: number, offset: number, limit: number) =>
    fb(() => request<SamplePage>('GET', `/results/${id}/samples`, { query: { offset, limit } }), () => mockResults.samples(id, offset, limit)),
  /** 结果集里的文件（图片等）通过中心服务代理读取，path 可以是相对结果集目录的路径或服务器上的绝对路径 */
  fileUrl: (id: number, path: string, evaluationId?: number) =>
    `/api/results/${id}/file?path=${encodeURIComponent(path)}${evaluationId ? `&evaluation_id=${evaluationId}` : ''}`,
  /** 样本里的图片字段；评测补上的 lq_image / ref_image 在评测服务器上，按 media 带上评测 ID */
  sampleFileUrl: (id: number, sample: Sample, field: 'image' | 'ref_image' | 'lq_image') =>
    resultsApi.fileUrl(id, String(sample[field]), sample.media?.[field]),
  /**
   * 结果集的全部图片列表（gzip，带 ETag）。传入上次的 etag 时带 If-None-Match，未变化返回 list 为 null；
   * 示例数据模式下 etag 固定为 'mock'
   */
  images: (id: number, opts: { signal?: AbortSignal; etag?: string } = {}) =>
    fb(
      async () => {
        const r = await requestFull<ImageList>('GET', `/results/${id}/images`, { signal: opts.signal, headers: opts.etag ? { 'If-None-Match': opts.etag } : undefined })
        return { list: r.data, etag: r.headers.get('ETag') ?? (r.status === 304 ? opts.etag ?? '' : '') }
      },
      () => ({ list: opts.etag === 'mock' ? null : mockResults.images(id), etag: 'mock' }),
    ),
  /**
   * 图片的缩略图 / 预览 / 原图 / 瓦片。参数顺序固定为 path, v, kind, size, l, x, y, evaluation_id，缺省的不出现；
   * 同一张图在各处生成的 URL 逐字节相同，浏览器缓存和请求去重都依赖这一点。v 为空时不带（不能长期缓存）
   */
  imageUrl: (id: number, path: string, v: string, kind: ImageKind, o: { size?: number; l?: number; x?: number; y?: number; evaluationId?: number } = {}) => {
    let q = `path=${encodeURIComponent(path)}`
    if (v) q += `&v=${encodeURIComponent(v)}`
    q += `&kind=${kind}`
    if (kind === 'preview') q += `&size=${previewBucket(o.size ?? 2048)}`
    if (kind === 'tile') q += `&l=${o.l ?? 0}&x=${o.x ?? 0}&y=${o.y ?? 0}`
    if (o.evaluationId) q += `&evaluation_id=${o.evaluationId}`
    return `/api/results/${id}/image?${q}`
  },
  evaluators: () => fb(() => request<Evaluator[]>('GET', '/evaluators'), () => mockResults.evaluators()),
  evaluations: (resultSetId?: number) =>
    fb(() => request<Evaluation[]>('GET', '/evaluations', { query: { result_set_id: resultSetId } }), () => mockResults.evaluations(resultSetId)),
  /** 只能删除已结束（成功或失败）的评测 */
  deleteEvaluation: (id: number) => fb(() => request<void>('DELETE', `/evaluations/${id}`), () => mockResults.deleteEvaluation(id)),
  evaluate: (body: EvaluationCreate) => fb(() => request<Evaluation>('POST', '/evaluations', { body }), () => mockResults.evaluate(body)),
  compareMetrics: (ids: number[]) =>
    fb(() => request<CompareMetrics>('GET', '/compare/metrics', { query: { ids: ids.map(String) } }), () => mockResults.compareMetrics(ids)),
  /** 样本顺序以 ids 中第一个结果集为准，asc/desc 也按第一个结果集的值排序 */
  compareSamples: (ids: number[], sortMetric: string | undefined, sort: CompareSort, offset = 0, limit = 20) =>
    fb(
      () => request<CompareSamples>('GET', '/compare/samples', { query: { ids: ids.map(String), sort_metric: sortMetric, sort, offset, limit } }),
      () => mockResults.compareSamples(ids, sortMetric, sort, offset, limit),
    ),
}
