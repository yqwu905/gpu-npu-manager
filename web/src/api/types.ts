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
  health_detail?: string | null
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
  agent_outdated: boolean
  managed: boolean
  deploy: DeployState | null
  ssh_user: string | null
  ssh_port: number
  ssh_host: string | null
  ssh_tunnel: boolean
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

export type DeployStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface DeployState {
  status: DeployStatus
  action: 'install' | 'upgrade' | null
  version: string | null
  error: string | null
  started_at: string | null
  finished_at: string | null
}

export interface DeployDetail extends DeployState {
  log: string | null
}

export interface AgentPackage {
  version: string
  ssh_available: boolean
  public_key: string | null
  public_key_path: string | null
  auto_upgrade: boolean
}

export interface ServerCreate {
  name?: string | null
  host: string
  port?: number
  ssh_user?: string | null
  ssh_port?: number
  ssh_host?: string | null
  ssh_tunnel?: boolean
  group?: string | null
  owner?: string | null
  tags?: string[]
  note?: string | null
  schedulable?: boolean
}

export type ServerUpdate = Partial<ServerCreate>

export interface SshConfigHosts {
  path: string
  hosts: { alias: string; hostname: string; user: string; port: number; added: boolean }[]
}

export interface ServerBatchResult {
  created: Server[]
  errors: { index: number; host: string; error: string }[]
}

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
  /** 排队中的任务为什么还没被调度，由每轮调度更新 */
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

/** 调度模式，切换只保存在中心服务内存中，重启后恢复为 GNM_SCHEDULE_STRICT 的值 */
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
  project_id: number | null
  project_name: string | null
  tags: string[]
  /** 每个指标最近一次成功评测的值 */
  metrics: Record<string, number>
  /** 最近一次带 LQ 基线的评测对应的 LQ 指标 */
  lq_metrics: Record<string, number>
  evaluating: boolean
  created_at: string
}

export interface ResultSetCreate {
  server_id: number
  path: string
  name?: string | null
  note?: string | null
  job_id?: number | null
  project_id?: number | null
  tags?: string[]
}

/** 只提交需要修改的字段，project_id 传 null 表示移出项目 */
export interface ResultSetUpdate {
  name?: string
  note?: string | null
  project_id?: number | null
  tags?: string[]
}

/** GET /api/results 的筛选条件；project_id 为 0 表示未归档 */
export interface ResultFilters {
  project_id?: number
  tag?: string[]
  q?: string
}

export interface Project {
  id: number
  name: string
  description: string | null
  result_count: number
  created_at: string
}

export interface TagCount {
  tag: string
  count: number
}

/** 评测配置；路径都在评测服务器上（未指定评测服务器时在结果所在服务器上） */
export interface EvalConfigBody {
  name: string
  metrics: string[]
  label_file: string | null
  gt_dir: string | null
  lq_dir: string | null
  /** 指定后先把结果目录拷贝到 server_path 下，再在该服务器上评测 */
  server_id: number | null
  server_path: string | null
  num_devices: number
  /** 运行评测脚本的 python 解释器，为空时用中心服务的 GNM_EVAL_PYTHON（默认 python3） */
  python: string | null
  note: string | null
}

export interface EvalConfig extends EvalConfigBody {
  id: number
  server_name: string | null
  created_at: string
  updated_at: string
}

/** predictions.jsonl 的一行原样返回（没有它时为扫描到的 image / text），常用字段 image / ref_image / text / ref_text，另加逐样本指标 */
export interface Sample {
  id?: string | number
  image?: string
  ref_image?: string
  text?: string
  ref_text?: string
  /** 评测配对到的 LQ 图片（评测服务器上的绝对路径） */
  lq_image?: string
  /** 评测时 OCR 识别出的文本（样本本身没有 text 时） */
  ocr_text?: string
  /** 来自评测服务器的图片字段 -> 评测 ID，读取文件时带上 */
  media?: Record<string, number>
  metrics: Record<string, number>
  [field: string]: unknown
}

export interface SamplePage {
  total: number
  offset: number
  items: Sample[]
}

export type EvaluationStatus = 'copying' | 'pending' | 'running' | 'succeeded' | 'failed'

export interface Evaluation {
  id: number
  result_set_id: number
  result_set_name: string
  metrics: string[]
  reference: string | null
  config_id: number | null
  config_name: string | null
  label_file: string | null
  gt_dir: string | null
  lq_dir: string | null
  python: string | null
  /** 运行评测的服务器 */
  server_id: number
  server_name: string
  /** 评测服务器上的结果目录（拷贝过去的，或结果集原目录） */
  data_path: string
  output_dir: string
  /** 本次是否计算 LQ 基线；同一配置只在第一次评测时计算 */
  compute_lq: boolean
  lq_source_id: number | null
  lq_values: Record<string, number | null> | null
  lq_counts: Record<string, number> | null
  lq_errors: Record<string, string> | null
  job_id: number | null
  job_status: string | null
  status: EvaluationStatus
  values: Record<string, number | null> | null
  counts: Record<string, number> | null
  errors: Record<string, string> | null
  num_skipped: number | null
  /** 运行中的进度，评测脚本每 10 秒更新一次；stage 为 lq 时在算 LQ 基线 */
  progress: { stage: 'lq' | 'main'; done: number; total: number; elapsed: number } | null
  error: string | null
  created_at: string
  finished_at: string | null
}

/** 给了 config_id 时先取配置中的值，其他字段覆盖配置 */
export interface EvaluationCreate {
  result_set_id: number
  config_id?: number | null
  metrics?: string[]
  reference?: string | null
  label_file?: string | null
  gt_dir?: string | null
  lq_dir?: string | null
  server_id?: number | null
  server_path?: string | null
  num_devices?: number
  python?: string | null
  /** 不复用同配置已有的 LQ 基线，本次重新计算 */
  recompute_lq?: boolean
  priority?: number
  submitter?: string | null
}

export interface CompareMetrics {
  result_sets: { id: number; name: string; server_name: string; meta: Record<string, unknown> }[]
  /** 至少一个结果集有值的指标 */
  metrics: Evaluator[]
  /** 结果集 ID -> 指标名 -> 值，缺少的指标没有键 */
  values: Record<string, Record<string, number>>
  /** 结果集 ID -> LQ 基线指标，没有时缺省 */
  lq_values: Record<string, Record<string, number>>
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
