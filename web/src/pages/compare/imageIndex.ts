// 结果集图片列表：模块级缓存最近用过的 12 个结果集（正在显示的不计入淘汰），去掉结果集再加回、在指标与图片视图间切换都不重新加载。
// 挂载时后台带 If-None-Match 重新验证，列表变了就换成新索引，由调用方按路径换算当前位置
import { useEffect, useRef, useState } from 'react'
import { resultsApi } from '../../api/results.ts'
import type { ImageList } from '../../api/types'
import { imageUrlParts, loader, type Meta } from './loader.ts'

/** flags 的位：已从响应头得知 | 浏览器能直接显示原图 | 浮点或 32 位数据按图归一化过 */
export const F_KNOWN = 1, F_NATIVE = 2, F_NORMALIZED = 4

export interface ImageIndex {
  id: number
  /** 列表的 ETag；示例数据为 'mock' */
  etag: string
  n: number
  /** 按 (文件名, 路径) 排好序，与后端一致 */
  paths: string[]
  names: string[]
  /** 版本号，'' 为未知 */
  vs: string[]
  /** 字节数；-1 文件不存在，NaN 未知 */
  sizes: Float64Array
  /** 原图宽高（EXIF 旋转后）：dims[2i]、dims[2i+1]，0 为未知；任一图片响应带回后就地补上 */
  dims: Uint32Array
  /** F_KNOWN | F_NATIVE | F_NORMALIZED，同样由响应头就地补上 */
  flags: Uint8Array
  /** 文件名 → 第一次出现的序号 */
  byName: Map<string, number>
  total: number
  missing: number
  skipped: number
  truncated: boolean
  source: ImageList['source']
}

/** 与后端 re.split(r"[\\/]", path)[-1] 相同 */
export const baseName = (p: string) => p.slice(Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')) + 1)

/** 一次遍历建好索引（2 万项约 5 ms） */
export function buildIndex(id: number, etag: string, list: ImageList): ImageIndex {
  const files = list.files, n = files.length
  const paths: string[] = Array(n), names: string[] = Array(n), vs: string[] = Array(n)
  const sizes = new Float64Array(n), byName = new Map<string, number>()
  for (let i = 0; i < n; i++) {
    const f = files[i], p = f[0], name = baseName(p)
    paths[i] = p
    names[i] = name
    vs[i] = f[2] || ''
    sizes[i] = typeof f[1] === 'number' ? f[1] : NaN
    if (!byName.has(name)) byName.set(name, i)
  }
  return {
    id, etag, n, paths, names, vs, sizes, dims: new Uint32Array(2 * n), flags: new Uint8Array(n), byName,
    total: list.total, missing: list.missing, skipped: list.skipped, truncated: list.truncated, source: list.source,
  }
}

const pathMaps = new WeakMap<ImageIndex, Map<string, number>>()
/** 路径 → 序号，不存在为 -1；路径表第一次用到时才建 */
export function indexOfPath(ix: ImageIndex, path: string): number {
  let m = pathMaps.get(ix)
  if (!m) {
    m = new Map()
    for (let i = 0; i < ix.n; i++) m.set(ix.paths[i], i)
    pathMaps.set(ix, m)
  }
  return m.get(path) ?? -1
}

/** 列表变化后换算当前位置：同一路径仍在就取它的新序号，否则取排序位置上紧随其后的一张 */
export function remapIndex(prev: ImageIndex, next: ImageIndex, i: number): number {
  if (!next.n) return 0
  const p = prev.paths[i]
  if (p === undefined) return Math.min(Math.max(0, i), next.n - 1)
  const j = indexOfPath(next, p)
  if (j >= 0) return j
  const name = prev.names[i]
  let lo = 0, hi = next.n
  while (lo < hi) {
    const mid = (lo + hi) >> 1, nm = next.names[mid]
    if (nm < name || (nm === name && next.paths[mid] < p)) lo = mid + 1
    else hi = mid
  }
  return Math.min(lo, next.n - 1)
}

/** 记下响应头带回的原图尺寸等信息 */
export function noteMeta(ix: ImageIndex, i: number, meta: Meta) {
  if (i < 0 || i >= ix.n || (!meta.W && !meta.format)) return
  if (meta.W && meta.H) {
    ix.dims[2 * i] = meta.W
    ix.dims[2 * i + 1] = meta.H
  }
  ix.flags[i] = F_KNOWN | (meta.native ? F_NATIVE : 0) | (meta.normalized ? F_NORMALIZED : 0)
}

/** 新旧列表里版本号相同（且已知）的图，沿用已知的尺寸信息 */
function carry(prev: ImageIndex, next: ImageIndex) {
  for (let i = 0; i < next.n; i++) {
    if (!next.vs[i]) continue
    const j = indexOfPath(prev, next.paths[i])
    if (j < 0 || prev.vs[j] !== next.vs[i]) continue
    next.dims[2 * i] = prev.dims[2 * j]
    next.dims[2 * i + 1] = prev.dims[2 * j + 1]
    next.flags[i] = prev.flags[j]
  }
}

interface Slot { ix: ImageIndex | null; p: Promise<ImageIndex>; rv: Promise<ImageIndex> | null }
export const MAX_CACHED = 12
const cache = new Map<number, Slot>()
/** 正在显示的结果集（引用计数），不淘汰 */
const pins = new Map<number, number>()

function touch(id: number, s: Slot) {
  cache.delete(id)
  cache.set(id, s)
  // 超出时淘汰最久没用的；正在显示的都留着，同时选了超过 12 个结果集时可以超出
  for (const k of cache.keys()) {
    if (cache.size <= MAX_CACHED) break
    if (!pins.has(k)) cache.delete(k)
  }
}

/** 页面正在显示这些结果集：移到最近使用，且不被淘汰；返回解除的函数 */
export function pinIndexes(ids: number[]): () => void {
  for (const id of ids) {
    pins.set(id, (pins.get(id) ?? 0) + 1)
    const s = cache.get(id)
    if (s) touch(id, s)
  }
  return () => {
    for (const id of ids) {
      const n = (pins.get(id) ?? 1) - 1
      if (n > 0) pins.set(id, n)
      else pins.delete(id)
    }
  }
}

/** 已加载的索引，没有为 null */
export const peekIndex = (id: number): ImageIndex | null => cache.get(id)?.ix ?? null

/** 读取（并缓存）结果集的图片索引；并发调用共用一个请求，失败的不缓存 */
export function loadIndex(id: number): Promise<ImageIndex> {
  const hit = cache.get(id)
  if (hit) {
    touch(id, hit)
    return hit.p
  }
  const s: Slot = { ix: null, p: null as unknown as Promise<ImageIndex>, rv: null }
  s.p = resultsApi.images(id).then(({ list, etag }) => (s.ix = buildIndex(id, etag, list ?? { total: 0, missing: 0, skipped: 0, truncated: false, source: 'agent', files: [] })))
  s.p.catch(() => { if (cache.get(id) === s) cache.delete(id) })
  touch(id, s)
  return s.p
}

/** 带 If-None-Match 重新验证；未变化返回原索引（同一对象），变化时换成新索引（沿用已知尺寸） */
export function revalidate(id: number): Promise<ImageIndex> {
  const s = cache.get(id)
  if (!s?.ix) return loadIndex(id)
  if (s.rv) return s.rv
  const prev = s.ix
  s.rv = resultsApi.images(id, { etag: prev.etag }).then(({ list, etag }) => {
    if (!list || etag === prev.etag) return prev
    const next = buildIndex(id, etag, list)
    carry(prev, next)
    if (cache.get(id) === s) {
      s.ix = next
      s.p = Promise.resolve(next)
    }
    return next
  })
  s.rv.then(() => { s.rv = null }, () => { s.rv = null })
  return s.rv
}

/** 清掉模块缓存（测试用） */
export function clearIndexCache() {
  cache.clear()
  pins.clear()
}

// 任何图片响应带回尺寸时补到对应索引上
loader.onMeta((url, meta) => {
  const u = imageUrlParts(url)
  const ix = u && cache.get(u.id)?.ix
  if (u && ix) noteMeta(ix, indexOfPath(ix, u.path), meta)
})

export interface IndexState { ix: ImageIndex | null; loading: boolean; error: string | null }
const LOADING: IndexState = { ix: null, loading: true, error: null }
const states = new WeakMap<ImageIndex, IndexState>()
/** 同一个索引总得到同一个状态对象，未变化的结果集不触发重渲染 */
const stateOf = (ix: ImageIndex) => {
  let s = states.get(ix)
  if (!s) states.set(ix, (s = { ix, loading: false, error: null }))
  return s
}

/**
 * 各结果集的图片索引。已缓存的同步可得（不闪“加载中”）；每次挂载对每个已缓存的结果集只重新验证一次，
 * 列表变化时先调 onSwap(id, 旧索引, 新索引) 再换成新状态，调用方据此用 remapIndex 换算当前位置
 */
export function useImageIndexes(ids: number[], onSwap?: (id: number, prev: ImageIndex, next: ImageIndex) => void): Record<number, IndexState> {
  const [map, setMap] = useState<Record<number, IndexState>>(() => {
    const m: Record<number, IndexState> = {}
    for (const id of ids) {
      const ix = peekIndex(id)
      m[id] = ix ? stateOf(ix) : LOADING
    }
    return m
  })
  const swap = useRef(onSwap)
  swap.current = onSwap
  const checked = useRef(new Set<number>())
  const key = ids.join(',')

  // 先 pin 住：下面加载新结果集时不会挤掉正在显示的
  useEffect(() => pinIndexes(ids), [key]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const put = (id: number, s: IndexState) => setMap((m) => (m[id] === s ? m : { ...m, [id]: s }))
    for (const id of ids) {
      const had = peekIndex(id)
      if (had) {
        put(id, stateOf(had))
        if (checked.current.has(id)) continue
        checked.current.add(id)
        revalidate(id).then((ix) => {
          if (ix === had) return
          swap.current?.(id, had, ix)
          put(id, stateOf(ix))
        }, () => {})
      } else {
        // 刚加载的列表不必再验证
        checked.current.add(id)
        put(id, LOADING)
        loadIndex(id).then((ix) => put(id, stateOf(ix)), (e) => {
          checked.current.delete(id)
          put(id, { ix: null, loading: false, error: e instanceof Error ? e.message : String(e) })
        })
      }
    }
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  return map
}
