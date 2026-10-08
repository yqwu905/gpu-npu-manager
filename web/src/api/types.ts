// 第 1 步已上线的接口，字段与 server/app/schemas.py 保持一致

export type ServerStatus = 'unknown' | 'online' | 'offline'
export type Accelerator = 'gpu' | 'npu'

export interface Process {
  pid: number | null
  name: string | null
  user: string | null
  memory_mb: number | null
}

export interface Device {
  index: number
  vendor: string
  model: string | null
  uuid: string | null
  bus_id: string | null
  npu_id: number | null
  chip_id: number | null
  memory_total_mb: number | null
  memory_used_mb: number | null
  utilization: number | null
  temperature: number | null
  power_w: number | null
  power_limit_w: number | null
  health: string | null
  processes: Process[]
  idle: boolean
  updated_at: string | null
  /** 第 2 步起提供：占用这张卡的平台任务 id，没有为 null */
  job_id?: number | null
}

export interface HostInfo {
  cpu_count: number | null
  cpu_percent: number | null
  load1: number | null
  memory_total_mb: number | null
  memory_used_mb: number | null
  disks: { path: string; total_gb: number | null; used_gb: number | null }[]
}

export interface Server {
  id: number
  name: string
  host: string
  port: number
  group: string | null
  owner: string | null
  tags: string[]
  note: string | null
  schedulable: boolean
  accelerator: Accelerator | null
  status: ServerStatus
  hostname: string | null
  agent_version: string | null
  host_info: HostInfo | null
  last_seen_at: string | null
  last_error: string | null
  models: string[]
  device_count: number
  idle_device_count: number
  devices: Device[]
  created_at: string
  updated_at: string
}

export interface ServerCreate {
  name: string
  host: string
  port?: number
  group?: string | null
  owner?: string | null
  tags?: string[]
  note?: string | null
  schedulable?: boolean
}

export type ServerUpdate = Partial<ServerCreate>

export interface ServerGroup {
  key: string | null
  server_count: number
  online_count: number
  device_count: number
  idle_device_count: number
  servers: Server[]
}

export type GroupBy = 'group' | 'owner' | 'tag' | 'accelerator' | 'model' | 'status'

export interface ServerQuery {
  q?: string
  group?: string[]
  owner?: string[]
  tag?: string[]
  accelerator?: Accelerator
  model?: string[]
  status?: ServerStatus
  schedulable?: boolean
  has_idle?: boolean
  include_devices?: boolean
}

export interface FilterOptions {
  groups: string[]
  owners: string[]
  tags: string[]
  models: string[]
  accelerators: string[]
}

export interface Overview {
  servers_total: number
  servers_online: number
  servers_offline: number
  devices_total: number
  devices_idle: number
  devices_busy: number
  by_accelerator: Record<string, { servers: number; devices: number; idle_devices: number }>
  /** 第 2 步起提供 */
  jobs_queued?: number
  jobs_running?: number
  jobs_lost?: number
}

export interface DeviceHistory {
  device_index: number
  points: { ts: string; utilization: number | null; memory_used_mb: number | null; temperature: number | null; power_w: number | null }[]
}

// ---- 第 2 步任务调度接口，字段与 server/app/schemas.py 一致 ----

export type JobStatus = 'queued' | 'starting' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'lost'

export interface Job {
  id: number
  name: string
  command: string
  workdir: string | null
  env: Record<string, string>
  num_devices: number
  group: string | null
  accelerator: Accelerator | null
  /** 提交时指定的服务器，可选 */
  server_id: number | null
  priority: number
  submitter: string | null
  status: JobStatus
  /** 排队中的任务在队列中的位置，从 1 开始 */
  queue_position: number | null
  assigned_server_id: number | null
  assigned_server_name: string | null
  device_indices: number[]
  pid: number | null
  exit_code: number | null
  error: string | null
  requeued_from: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** 提案：排队中的任务为什么还没被调度（后端暂未提供） */
  wait_reason?: string | null
}

export interface JobPage {
  total: number
  items: Job[]
}

export interface JobCreate {
  name?: string | null
  command: string
  workdir?: string | null
  env?: Record<string, string>
  num_devices: number
  group?: string | null
  accelerator?: Accelerator | null
  server_id?: number | null
  priority?: number
  submitter?: string | null
}

export interface JobQuery {
  status?: JobStatus[]
  group?: string
  submitter?: string
  server_id?: number
  q?: string
  limit?: number
  offset?: number
}

export interface JobLog {
  offset: number
  next_offset: number
  size: number
  data: string
}

/** 提案：调度模式查询与切换（后端目前只能用环境变量 GNM_SCHEDULE_STRICT 配置） */
export interface SchedulerSettings {
  strict_order: boolean
}

// ---- 第 3 步推理结果与评测接口，字段与 server/app/schemas.py 一致 ----

export type MetricKind = 'image' | 'text'

/** GET /api/evaluators：每个指标一项 */
export interface Evaluator {
  name: string
  label: string
  kind: MetricKind
  unit: string | null
  higher_is_better: boolean
  description: string
}

export interface ResultSet {
  id: number
  name: string
  server_id: number
  server_name: string
  path: string
  /** meta.json 的内容 */
  meta: Record<string, unknown>
  sample_count: number | null
  note: string | null
  /** 产生该结果的任务 */
  job_id: number | null
  /** 每个指标最近一次成功评测的值 */
  metrics: Record<string, number>
  evaluating: boolean
  created_at: string
}

export interface ResultSetCreate {
  server_id: number
  path: string
  name?: string | null
  note?: string | null
  job_id?: number | null
}

/** predictions.jsonl 的一行原样返回，常用字段 image / ref_image / text / ref_text，另加逐样本指标 */
export interface Sample {
  id?: string | number
  image?: string
  ref_image?: string
  text?: string
  ref_text?: string
  metrics: Record<string, number>
  [field: string]: unknown
}

export interface SamplePage {
  total: number
  offset: number
  items: Sample[]
}

export type EvaluationStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface Evaluation {
  id: number
  result_set_id: number
  result_set_name: string
  metrics: string[]
  reference: string | null
  job_id: number | null
  job_status: string | null
  status: EvaluationStatus
  values: Record<string, number | null> | null
  counts: Record<string, number> | null
  errors: Record<string, string> | null
  num_skipped: number | null
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface EvaluationCreate {
  result_set_id: number
  metrics: string[]
  reference?: string | null
  num_devices?: number
  priority?: number
  submitter?: string | null
}

export interface CompareMetrics {
  result_sets: { id: number; name: string; server_name: string; meta: Record<string, unknown> }[]
  /** 至少一个结果集有值的指标 */
  metrics: Evaluator[]
  /** 结果集 ID -> 指标名 -> 值，缺少的指标没有键 */
  values: Record<string, Record<string, number>>
}

export interface CompareSampleItem {
  id: string
  /** 结果集 ID -> 该样本的记录（含 metrics），缺失为 null */
  results: Record<string, Sample | null>
  /** 排序指标在各结果集之间的最大差值 */
  spread: number | null
}

export interface CompareSamples {
  total: number
  offset: number
  items: CompareSampleItem[]
}

export type CompareSort = 'spread' | 'asc' | 'desc'
