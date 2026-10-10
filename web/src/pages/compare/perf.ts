// 性能计数：计数本身始终开启（只是整数自增）；地址带 ?perf=1 时挂到 window.__cmpPerf，并打 performance.mark 供 e2e 脚本读取
import { bitmaps } from './bitmapCache.ts'

export interface LaneStat { inflight: number; max: number; started: number; aborted: number; done: number; failed: number }
const lane = (): LaneStat => ({ inflight: 0, max: 0, started: 0, aborted: 0, done: 0, failed: 0 })

export const perf = {
  enabled: false,
  lanes: { main: lane(), prefetch: lane(), thumb: lane() },
  total: { inflight: 0, max: 0 },
  decodes: { active: 0, max: 0, done: 0, failed: 0 },
  /** 组件渲染次数，例如 thumbPanel、thumbRow、cell */
  renders: {} as Record<string, number>,
  get bitmapBytes() {
    return bitmaps.used
  },
  get pinnedBytes() {
    return bitmaps.pinnedBytes()
  },
  get bitmapCount() {
    return bitmaps.size
  },
  render(name: string) {
    perf.renders[name] = (perf.renders[name] ?? 0) + 1
  },
  /** cmp:switch、cmp:paint:<id>:<tier> 等；未开启时不打点，避免 performance 条目无限增长 */
  mark(name: string) {
    if (perf.enabled && typeof performance !== 'undefined') performance.mark(name)
  },
  reset() {
    perf.lanes = { main: lane(), prefetch: lane(), thumb: lane() }
    perf.total = { inflight: 0, max: 0 }
    perf.decodes = { active: 0, max: 0, done: 0, failed: 0 }
    perf.renders = {}
  },
}

let installed = 0
/** 页面挂载时调用；返回卸载函数。只有地址带 perf=1 时才生效 */
export function installPerf(): () => void {
  if (typeof location === 'undefined' || !/[?&]perf=1\b/.test(location.search + location.hash)) return () => {}
  installed++
  perf.enabled = true
  ;(window as Window & { __cmpPerf?: typeof perf }).__cmpPerf = perf
  let done = false
  return () => {
    if (done) return
    done = true
    // 保留 window.__cmpPerf，便于卸载后读取最终计数
    if (--installed === 0) perf.enabled = false
  }
}
