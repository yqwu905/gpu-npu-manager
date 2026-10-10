// Alt+单击匹配的主线程入口：完整文件名用 byName 当帧查到；查不到的结果集交给 Worker 算编辑距离。
// Worker 是模块级单例，按引用计数保留（StrictMode 下重复挂载也只建一个），全部释放 5 秒后结束
import type { ImageIndex } from './imageIndex.ts'
import { nearest, prepareSet, type MatchSet } from './match.ts'
import type { MatchMsg, MatchReply } from './matchWorker.ts'

export interface MatchResult {
  /** 文件名完全相同的结果集 → 序号（取第一次出现的），可立即应用 */
  exact: Record<number, number>
  /** 其余结果集的编辑距离结果；期间又发起了新的匹配时得到 null，不要应用 */
  pending: Promise<Record<number, number> | null> | null
}

const MAX_SETS = 12
let worker: Worker | null = null
let refs = 0
let idle: ReturnType<typeof setTimeout> | null = null
let seq = 0
const sent = new Map<number, ImageIndex>()
const waiting = new Map<number, { done: (r: Record<number, number>) => void; local: () => Record<number, number> }>()
const memo = new Map<string, Record<number, number>>()
const prepared = new WeakMap<ImageIndex, MatchSet>()

const prep = (ix: ImageIndex) => {
  let s = prepared.get(ix)
  if (!s) prepared.set(ix, (s = prepareSet(ix.names)))
  return s
}

function stop() {
  idle = null
  worker?.terminate()
  worker = null
  sent.clear()
  // 未完成的查询改在主线程算完
  for (const w of waiting.values()) w.done(w.local())
  waiting.clear()
}

function ensureWorker(): Worker | null {
  if (worker) return worker
  try {
    worker = new Worker(new URL('./matchWorker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (e: MessageEvent<MatchReply>) => {
      const w = waiting.get(e.data.seq)
      if (w) { waiting.delete(e.data.seq); w.done(e.data.result) }
    }
    worker.onerror = () => stop()
  } catch {
    worker = null
  }
  return worker
}

function post(m: MatchMsg) {
  worker?.postMessage(m)
}

/** 把列表的文件名交给 Worker；同一个列表对象只传一次。超过 12 个结果集时丢掉最久没用的，keep 里的（正在用的）不丢 */
function sync(ix: ImageIndex, keep: Set<number>) {
  if (sent.get(ix.id) !== ix) post({ op: 'set', id: ix.id, names: ix.names })
  sent.delete(ix.id)
  sent.set(ix.id, ix)
  for (const id of sent.keys()) {
    if (sent.size <= MAX_SETS) break
    if (keep.has(id)) continue
    sent.delete(id)
    post({ op: 'drop', id })
  }
}

/** 页面挂载时调用，返回释放函数（可重复调用） */
export function retainMatcher(): () => void {
  refs++
  if (idle) { clearTimeout(idle); idle = null }
  let released = false
  return () => {
    if (released) return
    released = true
    if (--refs === 0) idle = setTimeout(stop, 5000)
  }
}

/** 列表加载后提前把当前各结果集的文件名交给 Worker，第一次 Alt+单击时不必再传 */
export function preloadMatch(ixs: ImageIndex[]) {
  const keep = new Set(ixs.map((ix) => ix.id))
  for (const ix of ixs) if (refs > 0 && ix.n && ensureWorker()) sync(ix, keep)
}

/**
 * 在 targets 里找与 q（src 中的文件名）最接近的图片。hints 为各结果集的种子序号（通常是同序号），只影响速度不影响结果；
 * 结果按 (q, 各列表版本) 记忆
 */
export function matchName(q: string, src: ImageIndex, targets: ImageIndex[], hints: Record<number, number> = {}): MatchResult {
  const exact: Record<number, number> = {}
  const rest: ImageIndex[] = []
  for (const t of targets) {
    const k = t.byName.get(q)
    if (k !== undefined) exact[t.id] = k
    else if (t.n) rest.push(t)
  }
  if (!rest.length) return { exact, pending: null }
  const key = `${q}\0${src.id}:${src.etag}\0${rest.map((t) => `${t.id}:${t.etag}`).join(',')}`
  const hit = memo.get(key)
  if (hit) return { exact: { ...exact, ...hit }, pending: null }
  const my = ++seq
  const local = () => {
    const s = prep(src), r: Record<number, number> = {}
    for (const t of rest) r[t.id] = nearest(q, prep(t), hints[t.id] ?? -1, s)
    return r
  }
  const pending = new Promise<Record<number, number>>((done) => {
    const w = refs > 0 ? ensureWorker() : null
    if (!w) { setTimeout(() => done(local()), 0); return }
    const keep = new Set([src.id, ...rest.map((t) => t.id)])
    sync(src, keep)
    for (const t of rest) sync(t, keep)
    waiting.set(my, { done, local })
    post({ op: 'nearest', seq: my, q, src: src.id, ids: rest.map((t) => t.id), hints })
  }).then((r) => {
    memo.set(key, r)
    if (memo.size > 200) memo.delete(memo.keys().next().value!)
    return my === seq ? r : null
  })
  return { exact, pending }
}
