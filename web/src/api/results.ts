import { request, withFallback } from './client'
import { mockResults } from '../mock/results'
import type {
  CompareMetrics, CompareSamples, CompareSort, EvalConfig, EvalConfigBody, Evaluation, EvaluationCreate, Evaluator, Project, ResultFilters,
  ResultSet, ResultSetCreate, ResultSetUpdate, Sample, SamplePage, TagCount,
} from './types'

const fb = <T>(real: () => Promise<T>, mock: () => T | Promise<T>) => withFallback('results', real, mock)

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
  evaluators: () => fb(() => request<Evaluator[]>('GET', '/evaluators'), () => mockResults.evaluators()),
  evaluations: (resultSetId?: number) =>
    fb(() => request<Evaluation[]>('GET', '/evaluations', { query: { result_set_id: resultSetId } }), () => mockResults.evaluations(resultSetId)),
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
