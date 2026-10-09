import { request, withFallback } from './client'
import { mockJobs } from '../mock/jobs'
import type { Job, JobCreate, JobLog, JobPage, JobQuery, SchedulerSettings } from './types'

const fb = <T>(real: () => Promise<T>, mock: () => T | Promise<T>) => withFallback('jobs', real, mock)

export const jobsApi = {
  list: (query: JobQuery = {}) => fb(() => request<JobPage>('GET', '/jobs', { query: { limit: 200, ...query } }), () => mockJobs.list(query)),
  get: (id: number) => fb(() => request<Job>('GET', `/jobs/${id}`), () => mockJobs.get(id)),
  create: (body: JobCreate) => fb(() => request<Job>('POST', '/jobs', { body }), () => mockJobs.create(body)),
  /** 失联任务需要 force=true 才能取消（进程可能仍在运行） */
  cancel: (id: number, force = false) =>
    fb(() => request<Job>('POST', `/jobs/${id}/cancel`, { query: { force: force || undefined } }), () => mockJobs.cancel(id)),
  /** 只能删除已结束的任务 */
  remove: (id: number) => fb(() => request<void>('DELETE', `/jobs/${id}`), () => mockJobs.remove(id)),
  requeue: (id: number) => fb(() => request<Job>('POST', `/jobs/${id}/requeue`), () => mockJobs.requeue(id)),
  setPriority: (id: number, priority: number) => fb(() => request<Job>('PATCH', `/jobs/${id}`, { body: { priority } }), () => mockJobs.setPriority(id, priority)),
  log: (id: number, offset: number) => fb(() => request<JobLog>('GET', `/jobs/${id}/log`, { query: { offset } }), () => mockJobs.log(id, offset)),
  // 调度模式接口尚未提供，单独降级，不影响任务接口
  scheduler: () => withFallback('scheduler', () => request<SchedulerSettings>('GET', '/scheduler'), () => mockJobs.scheduler()),
  setScheduler: (body: SchedulerSettings) =>
    withFallback('scheduler', () => request<SchedulerSettings>('PATCH', '/scheduler', { body }), () => mockJobs.setScheduler(body)),
}
