// 缩略图：请求得到 Blob 后转成 objectURL，LRU 最多 3000 项，引用计数大于 0（正挂在 <img> 上）的不撤销。
// Blob 一并留着，格子里要缩略图占位时可直接 createImageBitmap（约 1 ms），不必再请求
import { useEffect, useReducer, useRef } from 'react'
import { PRIO, loader, type Entry, type Handle, type Lane, type Meta } from './loader.ts'

interface Thumb { src: string; blob: Blob; meta: Meta; refs: number }
export const THUMB_CAP = 3000
const cache = new Map<string, Thumb>()

function trim(max: number) {
  if (cache.size <= max) return
  for (const [url, t] of cache) {
    if (cache.size <= max) break
    if (t.refs > 0) continue
    cache.delete(url)
    URL.revokeObjectURL(t.src)
  }
}

export const thumbs = {
  /** objectURL，没有为 null；不改 LRU 顺序 */
  peek: (url: string) => cache.get(url)?.src ?? null,
  blob: (url: string) => cache.get(url)?.blob ?? null,
  meta: (url: string) => cache.get(url)?.meta ?? null,
  /** 加一次引用并标为最近使用，返回 objectURL；没有缓存时返回 null */
  retain(url: string): string | null {
    const t = cache.get(url)
    if (!t) return null
    t.refs++
    cache.delete(url)
    cache.set(url, t)
    return t.src
  },
  release(url: string) {
    const t = cache.get(url)
    if (t && t.refs > 0) t.refs--
  },
  /** 放入请求结果（as 为 'blob' 的 Entry）；已有时沿用旧的 */
  put(url: string, e: Entry) {
    if (cache.has(url) || !e.blob) return
    cache.set(url, { src: URL.createObjectURL(e.blob), blob: e.blob, meta: e.meta, refs: 0 })
    trim(THUMB_CAP)
  },
  /** 请求一张缩略图（Blob）；完成后自动放进缓存 */
  load(url: string, prio: number = PRIO.thumbs, lane: Lane = 'thumb'): Handle {
    const h = loader.acquire(url, lane, prio, 'blob')
    h.promise.then((e) => thumbs.put(url, e), () => {})
    return h
  },
  /** 由缓存的 Blob 解码出位图，没有缓存时为 null */
  bitmap(url: string): Promise<ImageBitmap> | null {
    const b = cache.get(url)?.blob
    return b ? createImageBitmap(b) : null
  },
  /** 撤销未被引用的项直到不超过 max（页面卸载时 trim(500)） */
  trim,
  get size() {
    return cache.size
  },
}

// 格子要缩略图占位（位图）时，已在这里的直接用（缩略图栏显示过的 objectURL 同步可画，否则解码 Blob），不再请求
loader.setBlobSource((url) => {
  const t = cache.get(url)
  return t ? { blob: t.blob, meta: t.meta, src: t.src } : null
})

export interface ThumbState { src: string | null; error: string | null }
const NONE: ThumbState = { src: null, error: null }

/**
 * 缩略图 objectURL。url 为 null 时不请求（滚动中、文件不存在等）；卸载或换 url 时释放，未完成的请求随之中止。
 * prio 变化（可见行 3 / overscan 5）时调整排队顺序
 */
export function useThumb(url: string | null, prio: number = PRIO.thumbs): ThumbState {
  const [, bump] = useReducer((n: number) => n + 1, 0)
  const hold = useRef<Handle | null>(null)
  const prioRef = useRef(prio)
  prioRef.current = prio

  useEffect(() => {
    if (!url) return
    let retained = thumbs.retain(url) !== null
    let h: Handle | null = null
    if (!retained) {
      const hd = (h = hold.current = thumbs.load(url, prioRef.current))
      hd.promise.then(() => {
        if (hd.released) return
        retained = thumbs.retain(url) !== null
        bump()
      }, () => { if (!hd.released) bump() })
    }
    return () => {
      h?.release()
      if (hold.current === h) hold.current = null
      if (retained) thumbs.release(url)
    }
  }, [url])

  useEffect(() => { hold.current?.setPrio(prio) }, [prio])

  if (!url) return NONE
  const src = thumbs.peek(url)
  if (src) return { src, error: null }
  const err = hold.current?.error
  return err ? { src: null, error: err.message } : NONE
}
