import { request, withFallback } from './client'
import { mockResults } from '../mock/results'
import type { CompareMetrics, CompareSamples, CompareSort, Evaluation, EvaluationCreate, Evaluator, ResultSet, ResultSetCreate, SamplePage } from './types'

const fb = <T>(real: () => Promise<T>, mock: () => T | Promise<T>) => withFallback('results', real, mock)

export const resultsApi = {
  list: () => fb(() => request<ResultSet[]>('GET', '/results'), () => mockResults.list()),
  create: (body: ResultSetCreate) => fb(() => request<ResultSet>('POST', '/results', { body }), () => mockResults.create(body)),
  samples: (id: number, offset: number, limit: number) =>
    fb(() => request<SamplePage>('GET', `/results/${id}/samples`, { query: { offset, limit } }), () => mockResults.samples(id, offset, limit)),
  /** 结果集里的文件（图片等）通过中心服务代理读取，path 可以是相对结果集目录的路径或服务器上的绝对路径 */
  fileUrl: (id: number, path: string) => `/api/results/${id}/file?path=${encodeURIComponent(path)}`,
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
