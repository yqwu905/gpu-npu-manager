// 预取：按各结果集的当前位置与最近一次换图方向算出想要的请求（纯函数），再与已持有的句柄做差：
// 新的 acquire、优先级变了的 setPrio、不再需要的 release（排队中的直接丢弃，进行中的中止）
import { resultsApi } from '../../api/results.ts'
import { BUDGET } from './bitmapCache.ts'
import { F_KNOWN, F_NATIVE, type ImageIndex } from './imageIndex.ts'
import { PRIO, loader, ranked, type As, type Handle, type Lane } from './loader.ts'
import { fit, planLossless, previewDims, type View } from './geometry.ts'

export interface Want { url: string; lane: Lane; prio: number; as: As }
export interface PrefetchSet { ix: ImageIndex; i: number; evaluationId?: number }
export interface PrefetchInput {
  sets: PrefetchSet[]
  /** 最近一次换图的方向，默认 +1 */
  dir: 1 | -1
  /** 当前预览档位（previewSize 的结果） */
  size: number
  /** 解码缓存预算与当前用量（字节），默认 BUDGET 与 0 */
  budget?: number
  used?: number
  /** 放大（z > 1）时传入，预取 +1 张同一视口的无损层 */
  zoom?: { v: View; cw: number; ch: number; dpr: number }
}

const NATIVE_EXT = /\.(png|jpe?g|gif|webp)$/i
/** 某张图的原图宽高，未知时为 null */
const dimsOf = (ix: ImageIndex, i: number): [number, number] | null => (ix.dims[2 * i] ? [ix.dims[2 * i], ix.dims[2 * i + 1]] : null)
const url = (s: PrefetchSet, i: number, kind: 'thumb' | 'preview' | 'full' | 'tile', o: { size?: number; l?: number; x?: number; y?: number } = {}) =>
  resultsApi.imageUrl(s.ix.id, s.ix.paths[i], s.ix.vs[i], kind, { ...o, evaluationId: s.evaluationId })

/** 每个结果集除当前张外还能预取几张预览（0–3）：n × (1 + c) × 预览字节 ≤ 预算的一半 */
export function neighbourCount(n: number, previewBytes: number, budget: number): number {
  if (n <= 0 || previewBytes <= 0) return 3
  return Math.max(0, Math.min(3, Math.floor((0.5 * budget) / (n * previewBytes)) - 1))
}

/**
 * 预取清单：每个结果集 +dir 的预览（P2）与缩略图位图（P2，连按时作占位），预算允许时再加 −dir、+2dir 的预览（P4）；
 * 放大时再加 +dir 同一视口的无损层（P4，预算有余时）。跳过越界和文件不存在的项
 */
export function planPrefetch(p: PrefetchInput): Want[] {
  const out: Want[] = []
  const budget = p.budget ?? BUDGET
  let used = p.used ?? 0
  const pb = (() => {
    // 用第一张已知尺寸的图估算预览字节数，都未知时按 16:9
    for (const s of p.sets) {
      const d = dimsOf(s.ix, s.i)
      if (d) { const [w, h] = previewDims(d[0], d[1], p.size); return w * h * 4 }
    }
    return p.size * Math.round((p.size * 9) / 16) * 4
  })()
  const offs: [number, number][] = ([[p.dir, PRIO.next], [-p.dir, PRIO.near], [2 * p.dir, PRIO.near]] as [number, number][]).slice(0, neighbourCount(p.sets.length, pb, budget))
  const ok = (ix: ImageIndex, j: number) => j >= 0 && j < ix.n && ix.sizes[j] !== -1
  for (const s of p.sets) {
    for (const [d, prio] of offs) if (ok(s.ix, s.i + d)) out.push({ url: url(s, s.i + d, 'preview', { size: p.size }), lane: 'prefetch', prio, as: 'bitmap' })
    const j = s.i + p.dir
    if (ok(s.ix, j)) out.push({ url: url(s, j, 'thumb'), lane: 'prefetch', prio: PRIO.next, as: 'bitmap' })
  }
  const z = p.zoom
  if (z && z.v.z > 1) {
    for (const s of p.sets) {
      const j = s.i + p.dir
      const d = ok(s.ix, j) ? dimsOf(s.ix, j) : null
      if (!d) continue
      const [W, H] = d, flags = s.ix.flags[j]
      const plan = planLossless({
        W, H, size: s.ix.sizes[j], native: flags & F_KNOWN ? !!(flags & F_NATIVE) : NATIVE_EXT.test(s.ix.paths[j]),
        previewLossless: Math.max(W, H) <= p.size, previewLong: Math.max(...previewDims(W, H, p.size)),
        v: z.v, ft: fit(W, H, z.cw, z.ch), cw: z.cw, ch: z.ch, dpr: z.dpr, ring: 0,
      })
      if (plan.kind === 'full') {
        const est = W * H * 4
        if (used + est >= budget) continue
        used += est
        out.push({ url: url(s, j, 'full'), lane: 'prefetch', prio: PRIO.near, as: 'bitmap' })
      } else if (plan.kind === 'tiles') {
        // 按名次与其他结果集交错：预取车道只有 2 个名额，先让不同的图各解码一张
        for (const [k, t] of plan.tiles.entries()) {
          const est = 512 * 512 * 4
          if (used + est >= budget) break
          used += est
          out.push({ url: url(s, j, 'tile', { l: plan.level, x: t.x, y: t.y }), lane: 'prefetch', prio: ranked(PRIO.near, k), as: 'bitmap' })
        }
      }
    }
  }
  return out
}

export interface Prefetcher {
  /** 换成新的清单：先 acquire 新的，再 release 不要的，两边都有的同一请求不会被中止 */
  update(wants: Want[]): void
  /** 释放全部 */
  clear(): void
  readonly size: number
}

export function createPrefetcher(acquire: (url: string, lane: Lane, prio: number, as: As) => Handle = loader.acquire): Prefetcher {
  let held = new Map<string, { h: Handle; prio: number }>()
  return {
    update(wants) {
      const next = new Map<string, { h: Handle; prio: number }>()
      for (const w of wants) {
        const key = `${w.as} ${w.url}`
        const dup = next.get(key)
        if (dup) {
          if (w.prio < dup.prio) { dup.prio = w.prio; dup.h.setPrio(w.prio) }
          continue
        }
        const old = held.get(key)
        if (old) {
          held.delete(key)
          if (old.prio !== w.prio) { old.prio = w.prio; old.h.setPrio(w.prio) }
          next.set(key, old)
        } else next.set(key, { h: acquire(w.url, w.lane, w.prio, w.as), prio: w.prio })
      }
      for (const { h } of held.values()) h.release()
      held = next
    },
    clear() {
      for (const { h } of held.values()) h.release()
      held = new Map()
    },
    get size() {
      return held.size
    },
  }
}

/** 悬停预取：最多保留 max 个句柄，超出时先进先出释放 */
export function createHoverQueue(max = 4, acquire: (url: string, lane: Lane, prio: number, as: As) => Handle = loader.acquire) {
  const hs: Handle[] = []
  return {
    add(url: string, prio: number = PRIO.next) {
      if (hs.some((h) => h.url === url)) return
      hs.push(acquire(url, 'prefetch', prio, 'bitmap'))
      while (hs.length > max) hs.shift()!.release()
    },
    clear() {
      while (hs.length) hs.shift()!.release()
    },
  }
}
