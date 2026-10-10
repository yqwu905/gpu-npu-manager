// 图片请求调度：车道与优先级、同一 URL 只请求一次（引用计数）、全部释放即中止、503 与网络错误退避重试、失败结果缓存 30 秒、
// 解码排队（大图同时最多 2 张、小图 4 张、缩略图 8 张）、连按方向键时推迟预览与无损层。示例数据模式下不发请求，由 mockSource 生成。
// 同源 HTTP/1.1 只有 6 个连接：媒体最多 5 个，留 1 个给 /api JSON；页面走 h2/h3 时放宽
import { usingMock } from '../../api/client.ts'
import type { ImageKind } from '../../api/types'
import { MockHttpError, mockSource, type MockImage } from '../../mock/images.ts'
import { bitmaps, closeSource, type LRU } from './bitmapCache.ts'
import { TILE, bucket, previewDims } from './geometry.ts'
import { perf } from './perf.ts'

export type Lane = 'main' | 'prefetch' | 'thumb'
export type As = 'bitmap' | 'blob'
export interface Limits { total: number; thumb: number; prefetch: number }
export const LIMIT_H1: Limits = { total: 5, thumb: 3, prefetch: 2 }
export const LIMIT_H2: Limits = { total: 16, thumb: 8, prefetch: 6 }

/**
 * 优先级，数字小的先发：0 可见格子当前图的预览与缩略图占位 | 1 可见格子的无损层（视口内瓦片 / full） |
 * 2 前进方向 +1 的预览、悬停预取 | 3 可见缩略图行 | 4 −1、+2 的预览、放大时 +1 的无损层、瓦片外圈 | 5 缩略图 overscan
 */
export const PRIO = { current: 0, lossless: 1, next: 2, thumbs: 3, near: 4, overscan: 5 } as const
/**
 * 同一优先级内的第 k 个（小数部分，不越过下一级）：各格子按 k 交错排队，所有格子的第一个请求先于任何格子的第二个。
 * 多个结果集同时冷启动（如放大时换到 8K 图）时，5 个连接能落在 5 张不同的图上，节点可同时解码，而不是排在前两个格子的瓦片后面
 */
export const ranked = (prio: number, k: number) => prio + Math.min(k, 99) / 100

/** 对应响应头 X-Image-*；W、H 为原图（EXIF 旋转后）尺寸，0 为未知 */
export interface Meta { W: number; H: number; format: string; native: boolean; lossless: boolean; normalized: boolean; tile: number }
export type Source = ImageBitmap | HTMLImageElement
export interface Entry {
  url: string
  meta: Meta
  /** as 为 'blob' 时有 */
  blob: Blob | null
  /** as 为 'bitmap' 时有；放在 bitmaps 缓存里，被淘汰时会 close()，只在持有句柄（或自己 pin 住）期间绘制 */
  bitmap: Source | null
  /** 解码后的像素宽高（blob 为 0） */
  w: number
  h: number
  bytes: number
  /** 渲染器画过后置 true，用来限制每帧首次上传 GPU 的字节数 */
  drawn: boolean
}

export class LoadError extends Error {
  /** HTTP 状态码；0 为网络错误（已重试过）或已取消（句柄已释放） */
  status: number
  /** 503 的 Retry-After 秒数 */
  retryAfter: number
  constructor(status: number, message: string, retryAfter = 0) {
    super(message)
    this.name = 'LoadError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

export interface Handle {
  readonly url: string
  readonly as: As
  /** 释放前未完成时以 status 0 的 LoadError 拒绝 */
  readonly promise: Promise<Entry>
  /** 完成后同步可读：缓存命中时 acquire 返回前就已有值，同一帧即可绘制 */
  readonly entry: Entry | null
  readonly error: LoadError | null
  /** 0..1，仅 kind=full 按流读取时更新 */
  readonly progress: number
  readonly released: boolean
  setPrio(p: number): void
  /** 可重复调用；引用全部释放时排队的请求丢弃、进行中的中止 */
  release(): void
}

export interface LoaderDeps {
  fetch?: (url: string, init: RequestInit) => Promise<Response>
  decode?: (blob: Blob) => Promise<Source>
  /** 示例数据：像素 → 可绘制对象 / Blob */
  decodeRaw?: (img: MockImage) => Promise<Source>
  encodeRaw?: (img: MockImage) => Promise<Blob>
  mock?: () => boolean
  now?: () => number
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (t: unknown) => void
  /** 不给时按页面协议自动选择 LIMIT_H1 / LIMIT_H2 */
  limits?: Limits
  cache?: LRU<Entry>
  /** 已在别处缓存的 Blob（缩略图栏），位图句柄命中时直接解码，不再请求 */
  blobOf?: BlobSource
  /** 已在页面上显示过的 objectURL 同步得到可绘制的图（不可用时为 null），默认用 <img> */
  syncImage?: (src: string) => Source | null
}
/** src 为缩略图栏 <img> 用的 objectURL */
export type BlobSource = (url: string) => { blob: Blob; meta: Meta; src?: string } | null

export interface Loader {
  acquire(url: string, lane: Lane, prio: number, as: As): Handle
  /** 每次方向键 / 按钮换图时调用；两次间隔小于 120 ms 视为连按，预览与无损层（瓦片、原图）推迟到最后一次之后 100 ms 再请求 */
  noteStep(): void
  /** 任何图片响应带回尺寸等信息时回调（imageIndex 用它补齐 dims） */
  onMeta(fn: (url: string, meta: Meta) => void): () => void
  /** 中止全部请求并拒绝所有未完成的句柄（页面卸载时） */
  abortAll(): void
  /** 指定已缓存 Blob 的来源（thumbs.ts 注册） */
  setBlobSource(fn: BlobSource | null): void
  limits(): Limits
  stats(): { jobs: number; queued: number; waiting: number; inflight: Record<Lane | 'total', number>; decodeQueued: number; decoding: number; negative: number }
}

export const NEGATIVE_MS = 30_000
const NEGATIVE = new Set([404, 413, 415])
const MAX_TRIES = 5
const REPEAT_MS = 120
const SETTLE_MS = 100
const BIG_PX = 4_000_000
/** 同时解码的上限：4 MP 及以上的大图、其余的小图、缩略图 */
const DECODE_MAX = { big: 2, small: 4, tiny: 8 }
const LABEL: Record<number, string> = { 404: '文件不存在', 413: '图片过大', 415: '无法解码', 501: '无法生成预览', 503: '服务繁忙' }

/** 解析本模块生成的图片 URL（/api/results/{id}/image?...） */
export function imageUrlParts(url: string): { id: number; path: string; kind: ImageKind; size?: number; l?: number; x?: number; y?: number } | null {
  const m = /\/results\/(\d+)\/image\?(.*)$/.exec(url)
  if (!m) return null
  const q = new URLSearchParams(m[2])
  const num = (k: string) => (q.has(k) ? Number(q.get(k)) : undefined)
  return { id: Number(m[1]), path: q.get('path') ?? '', kind: (q.get('kind') as ImageKind | null) ?? 'thumb', size: num('size'), l: num('l'), x: num('x'), y: num('y') }
}

/** 页面或 /api 请求走的是 h2/h3 时可以放宽并发 */
export function multiplexed(): boolean {
  try {
    const es = [...performance.getEntriesByType('navigation'), ...performance.getEntriesByType('resource').filter((e) => e.name.includes('/api/')).slice(-8)]
    return es.some((e) => /^h[23]/.test((e as PerformanceResourceTiming).nextHopProtocol ?? ''))
  } catch {
    return false
  }
}

const metaOf = (h: Headers): Meta => ({
  W: Number(h.get('X-Image-Width')) || 0,
  H: Number(h.get('X-Image-Height')) || 0,
  format: h.get('X-Image-Format') ?? '',
  native: h.get('X-Image-Native') === '1',
  lossless: h.get('X-Lossless') === '1',
  normalized: h.get('X-Normalized') === '1',
  tile: Number(h.get('X-Tile-Size')) || 0,
})

const sourceSize = (s: Source): [number, number] => ('naturalWidth' in s ? [s.naturalWidth, s.naturalHeight] : [s.width, s.height])

async function defaultDecode(blob: Blob): Promise<Source> {
  try {
    return await createImageBitmap(blob, { imageOrientation: 'from-image' })
  } catch {
    // 个别格式 createImageBitmap 不支持时退回 <img>
    const src = URL.createObjectURL(blob)
    const img = new Image()
    img.src = src
    try {
      await img.decode()
      return img
    } catch {
      URL.revokeObjectURL(src)
      throw new LoadError(415, LABEL[415])
    }
  }
}

/** 已在页面上加载过的 objectURL：新 <img> 设上 src 即完整可用时返回它（标为共用，淘汰时不撤销 objectURL） */
function defaultSyncImage(src: string): Source | null {
  if (typeof Image === 'undefined') return null
  const img = new Image()
  img.src = src
  if (!img.complete || !img.naturalWidth) return null
  img.dataset.shared = '1'
  return img
}

const imageData = (r: MockImage) => new ImageData(r.data, r.w, r.h)

async function defaultEncodeRaw(r: MockImage): Promise<Blob> {
  if (typeof OffscreenCanvas !== 'undefined') {
    const c = new OffscreenCanvas(r.w, r.h)
    c.getContext('2d')!.putImageData(imageData(r), 0, 0)
    return c.convertToBlob()
  }
  const c = document.createElement('canvas')
  c.width = r.w
  c.height = r.h
  c.getContext('2d')!.putImageData(imageData(r), 0, 0)
  return new Promise((ok, no) => c.toBlob((b) => (b ? ok(b) : no(new LoadError(415, LABEL[415])))))
}

type State = 'queued' | 'wait' | 'fetch' | 'post' | 'decodeq' | 'decode'
interface Job {
  url: string
  kind: ImageKind
  size: number
  lane: Lane
  prio: number
  seq: number
  hs: Set<H>
  state: State
  /** 正在占用的车道 */
  ran: Lane | null
  ctrl: AbortController | null
  timer: unknown
  tries: number
  meta: Meta | null
  blob: Blob | null
  raw: MockImage | null
  /** 示例数据编码成 PNG 的进行中结果，多个 blob 句柄共用 */
  enc: Promise<Blob> | null
  /** abortAll 之后作废：正在解码的结果不进缓存 */
  dead: boolean
  loaded: number
  total: number
}

interface H extends Handle {
  lane: Lane
  prio: number
  job: Job | null
  entry: Entry | null
  error: LoadError | null
  released: boolean
  done(e: Entry): void
  fail(e: LoadError): void
}

export function createLoader(deps: LoaderDeps = {}): Loader {
  const dfetch = deps.fetch ?? ((u: string, i: RequestInit) => fetch(u, i))
  const ddecode = deps.decode ?? defaultDecode
  const ddecodeRaw = deps.decodeRaw ?? ((r: MockImage) => createImageBitmap(imageData(r)))
  const dencodeRaw = deps.encodeRaw ?? defaultEncodeRaw
  const dmock = deps.mock ?? (() => usingMock('results'))
  const now = deps.now ?? (() => performance.now())
  const later = deps.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  const cancelLater = deps.clearTimeout ?? ((t: unknown) => clearTimeout(t as ReturnType<typeof setTimeout>))
  const cache = deps.cache ?? bitmaps
  let blobOf = deps.blobOf ?? null
  const syncImage = deps.syncImage ?? defaultSyncImage

  const jobs = new Map<string, Job>()
  const queue = new Set<Job>()
  const decodeQ = new Set<Job>()
  const neg = new Map<string, { err: LoadError; until: number }>()
  const inflight: Record<Lane | 'total', number> = { main: 0, prefetch: 0, thumb: 0, total: 0 }
  const metaFns = new Set<(url: string, meta: Meta) => void>()
  const ABORTED = () => new LoadError(0, '已取消')
  let lim: Limits | null = deps.limits ?? null
  let redetected = !!deps.limits
  let seq = 0
  let pumpQueued = false
  let wakeAt = Infinity
  let wakeTimer: unknown = null
  let lastStep = -Infinity, prevStep = -Infinity
  const active = { big: 0, small: 0, tiny: 0 }

  const limits = () => (lim ??= multiplexed() ? LIMIT_H2 : LIMIT_H1)

  function makeHandle(url: string, lane: Lane, prio: number, as: As): H {
    let ok!: (e: Entry) => void, no!: (e: LoadError) => void
    const promise = new Promise<Entry>((a, b) => { ok = a; no = b })
    promise.catch(() => {})
    const h: H = {
      url, as, lane, prio, promise, job: null, entry: null, error: null, released: false,
      get progress() {
        const j = h.job
        return h.entry ? 1 : j && j.total ? Math.min(1, j.loaded / j.total) : 0
      },
      done(e) {
        h.job = null
        h.entry = e
        ok(e)
      },
      fail(e) {
        h.job = null
        h.error = e
        no(e)
      },
      setPrio(p) {
        if (h.released || p === h.prio) return
        h.prio = p
        if (h.job) { retune(h.job); schedulePump() }
      },
      release() {
        if (h.released) return
        h.released = true
        if (as === 'bitmap') cache.unpin(url)
        const j = h.job
        if (!j) return
        h.job = null
        j.hs.delete(h)
        no(ABORTED())
        if (!j.hs.size) cancel(j)
        else retune(j)
      },
    }
    return h
  }

  /** 作业的优先级与车道跟随优先级最高的句柄 */
  function retune(j: Job) {
    let best: H | null = null
    for (const h of j.hs) if (!best || h.prio < best.prio) best = h
    if (best) { j.prio = best.prio; j.lane = best.lane }
  }

  function cancel(j: Job) {
    switch (j.state) {
      case 'queued': queue.delete(j); break
      case 'wait': cancelLater(j.timer); break
      case 'fetch': j.ctrl?.abort(); perf.lanes[j.ran ?? j.lane].aborted++; break
      case 'decodeq': decodeQ.delete(j); break
      // 解码已开始：完成后照常放进缓存
      case 'decode': return
      case 'post': break
    }
    if (jobs.get(j.url) === j) jobs.delete(j.url)
  }

  function schedulePump() {
    if (pumpQueued) return
    pumpQueued = true
    queueMicrotask(pump)
  }

  const room = (lane: Lane, L: Limits) =>
    lane === 'main' || (inflight[lane] < L[lane] && inflight.thumb + inflight.prefetch < L.total - 1)

  /** 连按期间的预览与无损层推迟到最后一次换图后 SETTLE_MS（被跳过的序号释放句柄时直接丢弃）；返回推迟到的时间，不推迟为 0 */
  const deferUntil = (j: Job, t: number) =>
    j.kind !== 'thumb' && lastStep - prevStep < REPEAT_MS && t < lastStep + SETTLE_MS ? lastStep + SETTLE_MS : 0

  /** 可见格子当前图的请求（P0、P1）还在排队或进行中 */
  function urgentActive() {
    for (const j of jobs.values()) if (j.prio < PRIO.next && (j.state === 'fetch' || j.state === 'queued')) return true
    return false
  }

  function pump() {
    pumpQueued = false
    const L = limits()
    const t = now()
    let wake = Infinity
    // 眼前的图（P0、P1）没取完时，P3 及以后的（缩略图栏、−1/+2 的预取、瓦片外圈）先不发：冷图要节点整图解码（8K 约 1 秒），
    // 它们会占住节点的解码槽位，让眼前的图排在后面；+1 的预取与悬停（P2）照发。眼前的图取完后再发
    const urgent = urgentActive()
    while (inflight.total < L.total && queue.size) {
      let best: Job | null = null
      for (const j of queue) {
        if (!room(j.lane, L) || (urgent && j.prio > PRIO.next)) continue
        const d = deferUntil(j, t)
        if (d) { if (d < wake) wake = d; continue }
        if (!best || j.prio < best.prio || (j.prio === best.prio && j.seq < best.seq)) best = j
      }
      if (!best) break
      start(best)
    }
    if (wake < wakeAt) {
      if (wakeTimer !== null) cancelLater(wakeTimer)
      wakeAt = wake
      wakeTimer = later(() => { wakeTimer = null; wakeAt = Infinity; pump() }, Math.max(0, wake - t))
    }
  }

  function start(j: Job) {
    queue.delete(j)
    j.state = 'fetch'
    j.ran = j.lane
    inflight[j.lane]++
    inflight.total++
    const ps = perf.lanes[j.lane]
    ps.started++
    ps.max = Math.max(ps.max, ++ps.inflight)
    perf.total.max = Math.max(perf.total.max, ++perf.total.inflight)
    j.ctrl = new AbortController()
    void run(j)
  }

  function endFetch(j: Job, ok: boolean) {
    const lane = j.ran!
    j.ran = null
    inflight[lane]--
    inflight.total--
    perf.lanes[lane].inflight--
    perf.total.inflight--
    if (ok) perf.lanes[lane].done++
    if (ok && !redetected) {
      // 首个媒体响应之后资源计时里有了协议信息，再判断一次
      redetected = true
      if (multiplexed()) lim = LIMIT_H2
    }
    schedulePump()
  }

  function sleep(ms: number, signal: AbortSignal) {
    return new Promise<void>((ok, no) => {
      const onAbort = () => { cancelLater(t); no(ABORTED()) }
      const t = later(() => { signal.removeEventListener('abort', onAbort); ok() }, ms)
      signal.addEventListener('abort', onAbort, { once: true })
    })
  }

  async function mockFetch(j: Job, signal: AbortSignal) {
    const u = imageUrlParts(j.url)
    if (!u) throw new LoadError(404, LABEL[404])
    await sleep(mockSource.delay(j.kind), signal)
    try {
      j.raw = mockSource.render(u.id, u.path, j.kind, u)
    } catch (e) {
      throw e instanceof MockHttpError ? new LoadError(e.status, e.message) : e
    }
    j.meta = { ...j.raw.meta }
  }

  async function netFetch(j: Job, signal: AbortSignal) {
    const resp = await dfetch(j.url, { signal, priority: j.prio < 1 ? 'high' : 'low' })
    if (!resp.ok) {
      let detail = ''
      try {
        const d = await resp.json()
        if (typeof d?.detail === 'string') detail = d.detail
      } catch {
        // 非 JSON 错误体
      }
      throw new LoadError(resp.status, LABEL[resp.status] ?? (detail || `加载失败（HTTP ${resp.status}）`), Number(resp.headers.get('Retry-After')) || 0)
    }
    j.meta = metaOf(resp.headers)
    if (j.kind === 'full' && resp.body) {
      // 原图可能几十 MB，按流读取以便显示进度
      j.total = Number(resp.headers.get('Content-Length')) || 0
      j.loaded = 0
      const reader = resp.body.getReader()
      const chunks: Uint8Array[] = []
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        chunks.push(value)
        j.loaded += value.length
      }
      j.blob = new Blob(chunks, { type: resp.headers.get('Content-Type') ?? '' })
    } else j.blob = await resp.blob()
  }

  async function run(j: Job) {
    const signal = j.ctrl!.signal
    try {
      if (dmock()) await mockFetch(j, signal)
      else await netFetch(j, signal)
    } catch (e) {
      endFetch(j, false)
      if (signal.aborted) return
      const err = e instanceof LoadError ? e : new LoadError(0, '网络错误')
      // 503 与网络错误（连接断开、响应体读到一半）重试
      if ((err.status === 503 || err.status === 0) && ++j.tries < MAX_TRIES) {
        // 退避 1、2、4、8 秒，服务端给的 Retry-After 更长时听它的，最长 30 秒
        j.state = 'wait'
        const s = Math.min(30, Math.max(err.retryAfter, 2 ** (j.tries - 1)))
        j.timer = later(() => {
          if (jobs.get(j.url) !== j) return
          j.state = 'queued'
          queue.add(j)
          pump()
        }, s * 1000)
        return
      }
      fail(j, err)
      return
    }
    endFetch(j, true)
    if (signal.aborted) return
    void fetched(j)
  }

  function notifyMeta(url: string, meta: Meta) {
    for (const fn of metaFns) {
      try {
        fn(url, meta)
      } catch {
        // 监听方的错误不影响加载
      }
    }
  }

  function fail(j: Job, err: LoadError) {
    if (jobs.get(j.url) === j) jobs.delete(j.url)
    if (NEGATIVE.has(err.status)) neg.set(j.url, { err, until: now() + NEGATIVE_MS })
    perf.lanes[j.lane].failed++
    for (const h of j.hs) h.fail(err)
    j.hs.clear()
  }

  /** 把 Blob 交给 as 为 'blob' 的句柄（示例数据要先编码成 PNG，解码同时进行；编码只做一次） */
  async function giveBlob(j: Job, hs: H[]) {
    const blob = j.blob ?? (j.blob = await (j.enc ??= dencodeRaw(j.raw!)))
    const e: Entry = { url: j.url, meta: j.meta!, blob, bitmap: null, w: 0, h: 0, bytes: blob.size, drawn: false }
    for (const h of hs) {
      if (h.job !== j) continue
      j.hs.delete(h)
      h.done(e)
    }
  }

  async function fetched(j: Job) {
    notifyMeta(j.url, j.meta!)
    j.state = 'post'
    try {
      // 等待编码期间新挂上的 blob 句柄也要给到
      for (let blobs = [...j.hs].filter((h) => h.as === 'blob'); blobs.length; blobs = [...j.hs].filter((h) => h.as === 'blob')) await giveBlob(j, blobs)
    } catch {
      fail(j, new LoadError(415, LABEL[415]))
      return
    }
    if (jobs.get(j.url) !== j) return
    if ([...j.hs].some((h) => h.as === 'bitmap')) {
      j.state = 'decodeq'
      decodeQ.add(j)
      pumpDecode()
    } else jobs.delete(j.url)
  }

  /** 解码后的像素数估计：预览按档位、瓦片 512²、缩略图 256²、原图 W×H */
  function pixels(j: Job): number {
    const m = j.meta
    if (j.kind === 'tile') return TILE * TILE
    if (j.kind === 'thumb') return 256 * 256
    if (j.raw) return j.raw.w * j.raw.h
    if (j.kind === 'preview') {
      if (m?.W && m.H) { const [w, h] = previewDims(m.W, m.H, j.size); return w * h }
      return j.size * j.size * 0.5625
    }
    return m?.W && m.H ? m.W * m.H : Infinity
  }

  // 缩略图（256 px，约 1 ms）单独一类：换图时各格子的占位一起解码，不排在 4 个小图名额后面多等一帧
  const classOf = (j: Job): 'big' | 'small' | 'tiny' => (j.kind === 'thumb' ? 'tiny' : pixels(j) >= BIG_PX ? 'big' : 'small')

  function pumpDecode() {
    for (;;) {
      let best: Job | null = null
      for (const j of decodeQ) {
        const c = classOf(j)
        if (active[c] >= DECODE_MAX[c]) continue
        if (!best || j.prio < best.prio || (j.prio === best.prio && j.seq < best.seq)) best = j
      }
      if (!best) return
      decodeQ.delete(best)
      void decode(best)
    }
  }

  async function decode(j: Job) {
    j.state = 'decode'
    const c = classOf(j)
    active[c]++
    perf.decodes.max = Math.max(perf.decodes.max, ++perf.decodes.active)
    let src: Source | null = null
    let err: LoadError | null = null
    try {
      src = j.raw ? await ddecodeRaw(j.raw) : await ddecode(j.blob!)
    } catch (e) {
      err = e instanceof LoadError ? e : new LoadError(415, LABEL[415])
    }
    active[c]--
    perf.decodes.active--
    if (j.dead) {
      // 页面已卸载（abortAll）：结果直接丢掉，不再进缓存
      if (src) closeSource({ bitmap: src } as Entry)
      pumpDecode()
      return
    }
    if (err || !src) {
      perf.decodes.failed++
      fail(j, err ?? new LoadError(415, LABEL[415]))
      pumpDecode()
      return
    }
    perf.decodes.done++
    if (jobs.get(j.url) === j) jobs.delete(j.url)
    const [w, h] = sourceSize(src)
    let meta = j.meta!
    if (!meta.W || !meta.H) {
      // 原图响应可能不带尺寸头，用解码结果补上（换新对象，已交出去的不改）
      meta = { ...meta, W: w, H: h }
      notifyMeta(j.url, meta)
    }
    const e: Entry = { url: j.url, meta, blob: null, bitmap: src, w, h, bytes: w * h * 4, drawn: false }
    cache.set(j.url, e, e.bytes)
    // blob 句柄由 giveBlob 交付（示例数据的 PNG 编码可能还没完成），这里只交位图
    for (const hd of j.hs) if (hd.as === 'bitmap') hd.done(e)
    j.hs.clear()
    j.blob = null
    j.raw = null
    pumpDecode()
  }

  /**
   * 缩略图栏正显示着的缩略图：用同一个 objectURL 新建的 <img> 同步可用（浏览器的“可用图片列表”），直接放进缓存，
   * 换图后的下一帧就能画占位；createImageBitmap 要等回调，常常多等一两帧。objectURL 归缩略图缓存管，淘汰时不撤销
   */
  function shown(url: string): Entry | null {
    const pre = blobOf?.(url)
    const img = pre?.src ? syncImage(pre.src) : null
    if (!pre || !img) return null
    const [w, h] = sourceSize(img)
    const e: Entry = { url, meta: pre.meta, blob: null, bitmap: img, w, h, bytes: w * h * 4, drawn: false }
    cache.set(url, e, e.bytes)
    return e
  }

  function acquire(url: string, lane: Lane, prio: number, as: As): Handle {
    const h = makeHandle(url, lane, prio, as)
    // 先 pin 住：完成后放进缓存时即受保护，直到释放
    if (as === 'bitmap') cache.pin(url)
    const ng = neg.get(url)
    if (ng) {
      if (ng.until > now()) { h.fail(ng.err); return h }
      neg.delete(url)
    }
    if (as === 'bitmap') {
      const e = cache.get(url) ?? shown(url)
      if (e) { h.done(e); return h }
    }
    let j = jobs.get(url)
    if (!j) {
      const u = imageUrlParts(url)
      j = {
        url, kind: u?.kind ?? 'full', size: bucket(u?.size ?? 2048), lane, prio, seq: ++seq, hs: new Set(), state: 'queued', ran: null,
        ctrl: null, timer: null, tries: 0, meta: null, blob: null, raw: null, enc: null, dead: false, loaded: 0, total: 0,
      }
      jobs.set(url, j)
      const pre = as === 'bitmap' ? blobOf?.(url) : null
      if (pre) {
        // 缩略图栏已有 Blob：跳过请求直接解码（约 1 ms）
        j.blob = pre.blob
        j.meta = pre.meta
        j.state = 'post'
        const jj = j
        queueMicrotask(() => { if (jobs.get(url) === jj) void fetched(jj) })
      } else queue.add(j)
    }
    j.hs.add(h)
    h.job = j
    retune(j)
    if (as === 'blob' && (j.state === 'decodeq' || j.state === 'decode')) void giveBlob(j, [h]).catch(() => h.fail(new LoadError(415, LABEL[415])))
    schedulePump()
    return h
  }

  return {
    acquire,
    noteStep() {
      prevStep = lastStep
      lastStep = now()
    },
    onMeta(fn) {
      metaFns.add(fn)
      return () => { metaFns.delete(fn) }
    },
    abortAll() {
      for (const j of [...jobs.values()]) {
        const hs = [...j.hs]
        j.hs.clear()
        for (const h of hs) h.fail(ABORTED())
        j.dead = true
        cancel(j)
        jobs.delete(j.url)
      }
      neg.clear()
    },
    setBlobSource(fn) {
      blobOf = fn
    },
    limits,
    stats: () => ({
      jobs: jobs.size, queued: queue.size, waiting: [...jobs.values()].filter((j) => j.state === 'wait').length, inflight: { ...inflight },
      decodeQueued: decodeQ.size, decoding: active.big + active.small + active.tiny, negative: neg.size,
    }),
  }
}

/** 全页共用的调度器 */
export const loader: Loader = createLoader()
