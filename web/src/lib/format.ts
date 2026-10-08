import type { Device, EvaluationStatus, JobStatus, Server } from '../api/types'

export function relTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const sec = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (sec < 5) return '刚刚'
  if (sec < 60) return `${sec} 秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}

export function duration(fromIso: string | null, toIso?: string | null): string {
  if (!fromIso) return '-'
  const min = Math.max(0, Math.round(((toIso ? new Date(toIso).getTime() : Date.now()) - new Date(fromIso).getTime()) / 60000))
  if (min < 60) return `${min} 分`
  const h = Math.floor(min / 60)
  return min % 60 ? `${h} 小时 ${min % 60} 分` : `${h} 小时`
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export const gb = (mb: number | null | undefined, digits = 1) => (mb === null || mb === undefined ? '-' : (mb / 1024).toFixed(digits))
export const num = (v: number | null | undefined, digits = 0, unit = '') => (v === null || v === undefined ? '-' : v.toFixed(digits) + unit)
export const pct = (used: number | null | undefined, total: number | null | undefined) =>
  used === null || used === undefined || !total ? 0 : Math.max(0, Math.min(100, (used / total) * 100))

export interface Look { name: string; bg: string; bd: string; fg: string }

export const DEVICE_LOOK: Record<string, Look> = {
  idle: { name: '空闲', bg: '#E3F2E8', bd: '#2E8B57', fg: '#1B5E3A' },
  job: { name: '平台任务', bg: '#DCE7FC', bd: '#1D5BD6', fg: '#123F99' },
  busy: { name: '已占用', bg: '#FCEBD6', bd: '#C46A0B', fg: '#8A4A06' },
  external: { name: '外部进程', bg: '#FCEBD6', bd: '#C46A0B', fg: '#8A4A06' },
  unhealthy: { name: '健康告警', bg: '#FBE1DF', bd: '#C23B30', fg: '#8E2219' },
  offline: { name: '离线', bg: '#ECEDEB', bd: '#A3A8AE', fg: '#5E646D' },
}

/** 卡状态：任务占用信息（job_id）要等第 2 步接口提供，之前统一显示为“已占用” */
export function deviceState(server: Server, d: Device): keyof typeof DEVICE_LOOK {
  if (server.status !== 'online') return 'offline'
  if (d.health && d.health.toUpperCase() !== 'OK') return 'unhealthy'
  if (d.job_id) return 'job'
  if (d.idle) return 'idle'
  return 'job_id' in d ? 'external' : 'busy'
}

export const STATUS_LOOK: Record<Server['status'], { name: string; dot: string }> = {
  online: { name: '在线', dot: '#2E8B57' },
  offline: { name: '离线', dot: '#A3A8AE' },
  unknown: { name: '未连通', dot: '#C9A227' },
}

export const ACC_LOOK: Record<string, { name: string; bg: string; fg: string }> = {
  gpu: { name: 'GPU', bg: '#E6F0DA', fg: '#355F12' },
  npu: { name: 'NPU', bg: '#FBE3E3', fg: '#9B1F26' },
}

export const JOB_LOOK: Record<JobStatus, { name: string; bg: string; fg: string }> = {
  queued: { name: '排队中', bg: '#ECEDEB', fg: '#3A4048' },
  starting: { name: '启动中', bg: '#E9E3FA', fg: '#4A2E9C' },
  running: { name: '运行中', bg: '#DCE7FC', fg: '#123F99' },
  succeeded: { name: '成功', bg: '#E3F2E8', fg: '#1B5E3A' },
  failed: { name: '失败', bg: '#FBE1DF', fg: '#8E2219' },
  cancelled: { name: '已取消', bg: '#ECEDEB', fg: '#5E646D' },
  lost: { name: '失联', bg: '#FCEBD6', fg: '#8A4A06' },
}

export const EVAL_LOOK: Record<EvaluationStatus, { name: string; bg: string; fg: string }> = {
  pending: { name: '排队中', bg: '#ECEDEB', fg: '#3A4048' },
  running: { name: '评测中', bg: '#DCE7FC', fg: '#123F99' },
  succeeded: { name: '成功', bg: '#E3F2E8', fg: '#1B5E3A' },
  failed: { name: '失败', bg: '#FBE1DF', fg: '#8E2219' },
}

/** 可调度空闲卡：服务器在线且参与调度时的空闲卡数 */
export const schedulableIdle = (s: Server) => (s.status === 'online' && s.schedulable ? s.idle_device_count : 0)
