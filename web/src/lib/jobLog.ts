import type { JobLog } from '../api/types'

/** 首次打开任务或落后太多跳转最新时，从末尾拉取的字节数（512 KB） */
export const INITIAL_TAIL_BYTES = 512 * 1024
/** 增量追赶或实时追加时单次拉取的字节数（1 MB） */
export const FOLLOW_CHUNK_BYTES = 1024 * 1024
/** 点击“加载更早”时单次向前拉取的字节数（1 MB） */
export const OLDER_CHUNK_BYTES = 1024 * 1024
/** 开启“跟随最新输出”时页面保留的日志窗口上限（4 MB），超出时按块从头部裁掉，避免大日志拖慢页面 */
export const MAX_FOLLOW_WINDOW_BYTES = 4 * 1024 * 1024
/** 暂停跟随时自动增量缓冲的上限（16 MB），防止后台无限增长 */
export const MAX_PAUSED_BUFFER_BYTES = 16 * 1024 * 1024
/** 追平末尾后，运行中任务的轮询间隔（毫秒） */
export const POLL_INTERVAL_MS = 1000
/** 相邻小块合并阈值（256 KB），既减少块数又保持按块裁剪粒度 */
const MERGE_CHUNK_BYTES = 256 * 1024

export interface LogChunk {
  offset: number
  nextOffset: number
  text: string
}

export interface LogState {
  chunks: LogChunk[]
  headOffset: number
  tailOffset: number
  size: number
  initialized: boolean
}

export function emptyLogState(): LogState {
  return { chunks: [], headOffset: 0, tailOffset: 0, size: 0, initialized: false }
}

export function applyTail(res: JobLog): LogState {
  const chunks = res.data ? [{ offset: res.offset, nextOffset: res.next_offset, text: res.data }] : []
  return {
    chunks,
    headOffset: res.offset,
    tailOffset: res.next_offset,
    size: res.size,
    initialized: true,
  }
}

export function trimLogState(state: LogState, maxBytes: number): LogState {
  if (state.chunks.length <= 1 || state.tailOffset - state.headOffset <= maxBytes) return state
  let start = 0
  while (start < state.chunks.length - 1 && state.tailOffset - state.chunks[start].offset > maxBytes) {
    start++
  }
  if (start === 0) return state
  const chunks = state.chunks.slice(start)
  return {
    ...state,
    chunks,
    headOffset: chunks[0].offset,
  }
}

export function applyAppend(state: LogState, res: JobLog, trimToBytes?: number): LogState {
  // 日志文件被截断或重置时直接以最新响应为准
  if (res.size < state.tailOffset || res.offset < state.tailOffset) {
    return applyTail(res)
  }
  if (!res.data && res.next_offset === state.tailOffset) {
    if (res.size === state.size && state.initialized) return state
    return { ...state, size: res.size, initialized: true }
  }
  const chunks = state.chunks.slice()
  const last = chunks[chunks.length - 1]
  if (last && last.nextOffset === res.offset && last.nextOffset - last.offset < MERGE_CHUNK_BYTES) {
    chunks[chunks.length - 1] = {
      offset: last.offset,
      nextOffset: res.next_offset,
      text: last.text + res.data,
    }
  } else if (res.data) {
    chunks.push({ offset: res.offset, nextOffset: res.next_offset, text: res.data })
  }
  const headOffset = chunks.length > 0 ? chunks[0].offset : res.offset
  const next: LogState = {
    chunks,
    headOffset,
    tailOffset: res.next_offset,
    size: Math.max(res.size, res.next_offset),
    initialized: true,
  }
  return trimToBytes !== undefined ? trimLogState(next, trimToBytes) : next
}

export function applyPrepend(state: LogState, res: JobLog): LogState {
  if (!res.data) {
    return {
      ...state,
      headOffset: Math.min(state.headOffset, res.offset),
      size: Math.max(state.size, res.size),
    }
  }
  const chunk: LogChunk = { offset: res.offset, nextOffset: res.next_offset, text: res.data }
  const chunks = state.chunks.length > 0 ? [chunk, ...state.chunks] : [chunk]
  return {
    chunks,
    headOffset: res.offset,
    tailOffset: Math.max(state.tailOffset, res.next_offset),
    size: Math.max(state.size, res.size),
    initialized: true,
  }
}

export function needsTailJump(tailOffset: number, size: number, maxLagBytes = MAX_FOLLOW_WINDOW_BYTES): boolean {
  return size - tailOffset > maxLagBytes
}

export function logText(state: LogState): string {
  if (state.chunks.length === 0) return ''
  if (state.chunks.length === 1) return state.chunks[0].text
  return state.chunks.map((c) => c.text).join('')
}

export function loadedBytes(state: LogState): number {
  return Math.max(0, state.tailOffset - state.headOffset)
}

export function formatLogBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}
