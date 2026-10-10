// 每个格子一个命令式 canvas 渲染器，不经过 React：底层画预览（还没有时画放大的缩略图），放大后叠加无损层（整张原图或瓦片）。
// 文件名、aria-label、data-tier、data-level 和角标都由这里写，始终描述画布上真正画出来的像素
import { resultsApi } from '../../api/results.ts'
import type { ImageKind } from '../../api/types'
import { BUDGET } from './bitmapCache.ts'
import { F_KNOWN, F_NATIVE, F_NORMALIZED, type ImageIndex } from './imageIndex.ts'
import { PRIO, loader, ranked, type Entry, type Handle, type LoadError } from './loader.ts'
import { fit, planLossless, previewDims, previewSize, scaleK, snapRect, tileRect, visibleRect, visibleTiles, type View } from './geometry.ts'
import { perf } from './perf.ts'
import type { ViewStore } from './viewStore.ts'

export interface CellImage { id: number; ix: ImageIndex; i: number }
/** 渲染器直接改写的 DOM 节点 */
export interface CellEls { vp: HTMLElement; canvas: HTMLCanvasElement; name: HTMLElement; tier: HTMLElement; flag: HTMLElement; ph: HTMLElement }
/** 已生效的格子 CSS 尺寸、dpr 与对应的预览档位 */
export interface CellGeom { cw: number; ch: number; dpr: number; pv: number }

interface Tile { h: Handle; l: number; x: number; y: number; ring: boolean }
/** 一张图在格子里的全部来源（句柄都 pin 着位图） */
export interface Pic {
  id: number
  ix: ImageIndex
  i: number
  path: string
  name: string
  v: string
  /** 字节数；-1 文件不存在（不请求），NaN 未知 */
  size: number
  thumb: Handle | null
  preview: Handle | null
  /** 格子尺寸变了换档时的旧预览，新的到之前继续画 */
  oldPreview: Handle | null
  pv: number
  full: Handle | null
  /** URL → 瓦片；当前层之外只留已解码的旧层，当前层画全后释放 */
  tiles: Map<string, Tile>
  level: number
  /** 预览返回 501：改用原图 */
  noPreview: boolean
  /** 瓦片返回 501：原图不大时改用原图，否则只显示预览 */
  noTiles: boolean
  onlyPreview: boolean
  /** 已打过 cmp:paint 标记的最高层级 */
  painted: number
}

export interface CellRenderer {
  /** 换图：先请求新图的来源再释放旧的；新图一点都没解码时旧画面（连同文件名）最多保留 100 ms */
  setImage(img: CellImage | null): void
  /** 按住对比：立即用对方格子已 pin 住的位图重画，不发请求；null 恢复 */
  setHold(other: CellRenderer | null): void
  /** 格子尺寸变化：过渡期间只拉伸画布，最后一次变化 120 ms 后（或折叠动画结束时）才重新分配；devW、devH 为浏览器给出的设备像素尺寸 */
  resize(cssW: number, cssH: number, dpr: number, devW?: number, devH?: number): void
  setVisible(v: boolean): void
  geom(): CellGeom | null
  /** 结果集名称等文字变了，下一帧重写 */
  relabel(): void
  draw(): void
  dispose(): void
  /** 当前画面上的图，按住对比时对方读取 */
  pic(): Pic | null
  /** 被按住对比时由对方每帧调用：自己不可见（不重画）时也按当前视图补齐无损层 */
  sync(): void
  /** 当前图该有的层都已到达（或失败）；不可见的格子算已就绪 */
  settled(): boolean
  watch(r: CellRenderer, on: boolean): void
}

export interface CellOpts {
  /** 预取要重算：生效的预览档位变了（格子尺寸或 dpr 变化），或当前图加载完毕（放大时才预取下一张的无损层） */
  onPrefetch?: () => void
}

const STALE_MS = 100
const SETTLE_MS = 120
const LEVEL_MS = 60
const UPLOAD_MAX = 24 * 1024 * 1024
export const TIERS = ['', 'thumb', 'preview', 'lossless'] as const

// 每帧首次上传 GPU 的位图不超过 24 MB，超出的推迟到下一帧（期间先画已有的更粗一层）；单张超过上限时独占一帧。
// force：格子里没有更粗的层可垫底时照样上传，避免空白一帧
let uploaded = 0
function admit(e: Entry, force: boolean): boolean {
  if (e.drawn) return true
  if (!force && uploaded > 0 && uploaded + e.bytes > UPLOAD_MAX) return false
  // 同一帧里所有格子都在一个 rAF 回调中重画，回调结束后的微任务清零
  if (!uploaded) queueMicrotask(() => { uploaded = 0 })
  uploaded += e.bytes
  e.drawn = true
  return true
}

function has(p: Pic): boolean {
  if (p.thumb?.entry || p.preview?.entry || p.oldPreview?.entry || p.full?.entry) return true
  for (const t of p.tiles.values()) if (t.h.entry) return true
  return false
}

function dimsOf(p: Pic): [number, number] | null {
  const W = p.ix.dims[2 * p.i], H = p.ix.dims[2 * p.i + 1]
  if (W && H) return [W, H]
  for (const h of [p.preview, p.oldPreview, p.thumb, p.full]) {
    const m = h?.entry?.meta
    if (m?.W && m.H) return [m.W, m.H]
  }
  return null
}

const metaOf = (p: Pic) => (p.preview?.entry ?? p.thumb?.entry ?? p.full?.entry)?.meta
function nativeOf(p: Pic): boolean {
  const f = p.ix.flags[p.i]
  return f & F_KNOWN ? !!(f & F_NATIVE) : !!metaOf(p)?.native
}
const normalizedOf = (p: Pic) => !!(p.ix.flags[p.i] & F_NORMALIZED) || !!metaOf(p)?.normalized
// 释放时的“已取消”不算失败；网络错误（status 0）照样显示
const failed = (h: Handle | null) => (h?.error && !h.released ? h.error : null)
const waiting = (h: Handle | null) => !!h && !h.entry && !h.error

function releasePic(p: Pic) {
  for (const h of [p.thumb, p.preview, p.oldPreview, p.full]) h?.release()
  for (const t of p.tiles.values()) t.h.release()
  p.tiles.clear()
}

/** 角标：描述实际画出的层；还在下载原图时显示进度 */
export function badgeOf(p: Pic, tier: number, level: number): string {
  if (p.size === -1) return '文件不存在'
  if (tier === 3) return level > 0 ? `无损 1/${2 ** level}` : '原始像素'
  const f = p.full
  if (f && !f.entry && !f.error) return `${p.noPreview ? '无法生成预览 · ' : ''}原图 ${Math.round(f.progress * 100)}%`
  if (tier === 2) return p.onlyPreview ? '仅显示预览' : '预览'
  const err = failed(p.preview) ?? failed(f) ?? failed(p.thumb)
  if (err) return err.message
  const loading = !!p.preview && !p.preview.entry && !p.preview.error
  return tier === 1 && !loading ? '缩略图' : '加载中'
}

export function createCellRenderer(els: CellEls, store: ViewStore, nameOf: (id: number) => string, opts: CellOpts = {}): CellRenderer {
  const ctx = els.canvas.getContext('2d')!
  let cur: Pic | null = null
  let stale: Pic | null = null
  let held: CellRenderer | null = null
  const watchers = new Set<CellRenderer>()
  let visible = true
  let disposed = false
  let cw = 0, ch = 0, dpr = 1, bw = 0, bh = 0
  let pending: [number, number, number, number, number] | null = null
  let lastView: View | null = null
  let lastMove = -Infinity
  let staleTimer: ReturnType<typeof setTimeout> | null = null
  let settleTimer: ReturnType<typeof setTimeout> | null = null
  let levelTimer: ReturnType<typeof setTimeout> | null = null
  let progTimer: ReturnType<typeof setTimeout> | null = null
  let done = false
  const written = new Map<HTMLElement, Record<string, string>>()

  const changed = () => {
    if (disposed) return
    store.mark(self)
    for (const w of watchers) store.mark(w)
  }
  const urlOf = (p: Pic, kind: ImageKind, o?: { size?: number; l?: number; x?: number; y?: number }) => resultsApi.imageUrl(p.id, p.path, p.v, kind, o)
  const own = () => (cur && stale && !has(cur) ? stale : cur)

  function dropStale() {
    if (staleTimer) clearTimeout(staleTimer)
    staleTimer = null
    if (!stale) return
    releasePic(stale)
    stale = null
    changed()
  }

  /** 来源到了就重画；当前图有了任何可画的内容即丢掉旧画面 */
  function track(p: Pic, h: Handle, onFail?: (e: LoadError) => void): Handle {
    h.promise.then(() => {
      if (h === p.preview && p.oldPreview) { p.oldPreview.release(); p.oldPreview = null }
      if (p === cur && stale) dropStale()
      changed()
    }, (e: LoadError) => {
      if (h.released) return
      onFail?.(e)
      changed()
    })
    return h
  }

  function setPreview(p: Pic) {
    if (p.size === -1 || !cw) return
    const s = previewSize(cw, ch, dpr)
    if (p.pv === s) return
    p.pv = s
    p.oldPreview?.release()
    p.oldPreview = null
    if (p.preview?.entry) p.oldPreview = p.preview
    else p.preview?.release()
    // 排在各格子的缩略图占位之后：冷启动时先让每个格子各有一个请求在节点上解码
    p.preview = track(p, loader.acquire(urlOf(p, 'preview', { size: s }), 'main', ranked(PRIO.current, 1), 'bitmap'), (e) => { if (e.status === 501) p.noPreview = true })
  }

  function newPic({ id, ix, i }: CellImage): Pic {
    const p: Pic = {
      id, ix, i, path: ix.paths[i], name: ix.names[i], v: ix.vs[i], size: ix.sizes[i], thumb: null, preview: null, oldPreview: null, pv: 0,
      full: null, tiles: new Map(), level: -1, noPreview: false, noTiles: false, onlyPreview: false, painted: 0,
    }
    if (p.size === -1) return p
    // 缩略图作占位：主车道 P0，缩略图栏已有 Blob 时直接解码
    p.thumb = track(p, loader.acquire(urlOf(p, 'thumb'), 'main', PRIO.current, 'bitmap'))
    setPreview(p)
    return p
  }

  function releaseTiles(p: Pic, drop: (url: string, t: Tile) => boolean) {
    for (const [url, t] of p.tiles) {
      if (!drop(url, t)) continue
      t.h.release()
      p.tiles.delete(url)
    }
  }

  function wantFull(p: Pic, prio: number) {
    if (!p.full) p.full = track(p, loader.acquire(urlOf(p, 'full'), 'main', prio, 'bitmap'))
  }

  /** 无损层：按 planLossless 取整张原图或当前层的可见瓦片（加一圈）；层级变化在手势中防抖 60 ms */
  function plan(p: Pic, v: View, now: number) {
    if (p.size === -1 || !cw) return
    const d = dimsOf(p)
    if (p.noPreview) {
      // 生成不了预览（没有图像库）：原图解码后不超过预算一半时直接用原图
      if (!(d && d[0] * d[1] * 4 > BUDGET / 2)) wantFull(p, PRIO.current)
      return
    }
    if (!d) return
    const [W, H] = d, ft = fit(W, H, cw, ch), pe = p.preview?.entry
    let lp = planLossless({
      W, H, size: p.size, native: nativeOf(p), previewLossless: pe ? pe.meta.lossless : Math.max(W, H) <= p.pv,
      previewLong: pe ? Math.max(pe.w, pe.h) : Math.max(...previewDims(W, H, p.pv)), v, ft, cw, ch, dpr,
    })
    p.onlyPreview = false
    if (lp.kind === 'tiles' && p.noTiles) {
      if (W * H * 4 <= BUDGET / 2) lp = { kind: 'full' }
      else { lp = { kind: 'none' }; p.onlyPreview = true }
    }
    if (lp.kind === 'full') wantFull(p, PRIO.lossless)
    else if (p.full) { p.full.release(); p.full = null }
    if (lp.kind !== 'tiles') {
      releaseTiles(p, () => true)
      p.level = -1
      return
    }
    if (p.level !== lp.level) {
      // 换到更细的层（放大）在手势中防抖；换到更粗的层（缩小）立即切换，否则旧的细层在缩小时新露出的瓦片按平方增长
      const wait = lastMove + LEVEL_MS - now
      if (wait <= 0 || (p.level >= 0 && lp.level > p.level)) p.level = lp.level
      else if (!levelTimer) levelTimer = setTimeout(() => { levelTimer = null; changed() }, wait + 1)
    }
    if (p.level < 0) return
    // 层级还在防抖时，旧层在新视口里新露出的瓦片照样立即请求
    const want = p.level === lp.level ? lp.tiles : visibleTiles(W, H, p.level, v, ft, cw, ch, 1)
    const keep = new Set<string>()
    let done = true, inner = 0, ring = 0
    for (const t of want) {
      const url = urlOf(p, 'tile', { l: p.level, x: t.x, y: t.y })
      // want 已按离中心由近到远排好；同一级内按名次与其他格子交错
      const prio = t.ring ? ranked(PRIO.near, ring++) : ranked(PRIO.lossless, inner++)
      keep.add(url)
      let have = p.tiles.get(url)
      if (!have) {
        have = { h: track(p, loader.acquire(url, 'main', prio, 'bitmap'), (e) => { if (e.status === 501) p.noTiles = true }), l: p.level, x: t.x, y: t.y, ring: t.ring }
        p.tiles.set(url, have)
      } else {
        have.ring = t.ring
        have.h.setPrio(prio)
      }
      if (!t.ring && !have.h.entry && !have.h.error) done = false
    }
    // 当前层不再可见的释放（位图仍在缓存里，平移回来直接命中）；旧层未完成的立即释放，已解码的留到当前层画全
    const L = p.level
    releaseTiles(p, (url, t) => (t.l === L ? !keep.has(url) : done || !t.h.entry))
  }

  function paint(p: Pic | null, v: View) {
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, bw, bh)
    const out = { tier: 0, level: -1, defer: false }
    const d = p && dimsOf(p)
    if (!p || !d) return out
    const [W, H] = d, ft = fit(W, H, cw, ch), k = scaleK(v, ft, dpr)
    const vr = visibleRect(v, ft, cw, ch, W, H)
    if (!vr) return out
    // exact：来源就是原图像素（原图、第 0 层瓦片、无损预览）；cover：无损层（原图、瓦片）替换下面垫底的内容，
    // 先清空所在矩形，带透明度的图不与预览或更粗的层叠加
    const put = (e: Entry | null | undefined, x0: number, y0: number, x1: number, y1: number, exact: boolean, force = false, cover = false) => {
      if (!e?.bitmap) return false
      if (x1 <= vr[0] || x0 >= vr[2] || y1 <= vr[1] || y0 >= vr[3]) return true
      if (!admit(e, force)) { out.defer = true; return false }
      // 只有原图像素且一个原图像素不小于一个设备像素时关掉插值，像素边缘清晰；垫底的粗层放大时照常插值。
      // 插值质量每次都设（画布改尺寸后会复位）：medium 缩小时用 mipmap，大图缩得很小时不出摩尔纹
      ctx.imageSmoothingEnabled = !(exact && k >= 1)
      ctx.imageSmoothingQuality = 'medium'
      const [dx, dy, dw, dh] = snapRect(v, ft, dpr, x0, y0, x1, y1)
      if (cover) ctx.clearRect(dx, dy, dw, dh)
      ctx.drawImage(e.bitmap, dx, dy, dw, dh)
      return true
    }
    const pe = p.preview?.entry ?? p.oldPreview?.entry, te = p.thumb?.entry
    if (pe && put(pe, 0, 0, W, H, pe.meta.lossless, !te)) out.tier = pe.meta.lossless ? 3 : 2
    else if (put(te, 0, 0, W, H, false, true)) out.tier = 1
    if (out.tier === 3) out.level = 0
    if (put(p.full?.entry, 0, 0, W, H, true, false, true)) { out.tier = 3; out.level = 0 }
    if (p.tiles.size) {
      // 先画粗层再画细层：细层还没到的地方由粗层或预览垫底
      let need = 0, got = 0
      const ts = [...p.tiles.values()].sort((a, b) => b.l - a.l)
      for (const t of ts) {
        const inner = t.l === p.level && !t.ring
        if (inner) need++
        const [x0, y0, x1, y1] = tileRect(W, H, t.l, t.x, t.y)
        if (put(t.h.entry, x0, y0, x1, y1, t.l === 0, false, true) && inner) got++
      }
      if (need && got === need) { out.tier = 3; out.level = p.level }
    }
    return out
  }

  /** 当前图还有要等的层：预览（没有预览时的缩略图）、原图、当前层级的视口内瓦片，或层级切换还在防抖 */
  function busy(p: Pic): boolean {
    if (p.size === -1) return false
    if (waiting(p.preview) || waiting(p.full) || levelTimer || (!p.preview?.entry && waiting(p.thumb))) return true
    for (const t of p.tiles.values()) if (t.l === p.level && !t.ring && waiting(t.h)) return true
    return false
  }

  /** 只在值变化时写 DOM */
  function setDom(el: HTMLElement, key: string, val: string) {
    let m = written.get(el)
    if (!m) written.set(el, (m = {}))
    if (m[key] === val) return
    m[key] = val
    if (key === 'text') el.textContent = val
    else el.setAttribute(key, val)
  }

  /** 文件名、aria-label 与 data-pic（画出的是哪个结果集的第几张，“结果集:序号”）；格子不可见时也更新 */
  function naming(p: Pic | null) {
    setDom(els.name, 'text', p ? p.name : '-')
    setDom(els.name, 'title', p ? p.path : '')
    setDom(els.canvas, 'aria-label', p ? `${nameOf(p.id)} ${p.name}` : '无图片')
    setDom(els.canvas, 'data-pic', p ? `${p.id}:${p.i}` : '')
  }

  function label(p: Pic | null, tier: number, level: number) {
    naming(p)
    setDom(els.canvas, 'data-tier', TIERS[tier])
    setDom(els.canvas, 'data-level', tier === 3 ? String(level) : '')
    const b = p ? badgeOf(p, tier, level) : ''
    setDom(els.tier, 'text', b)
    if (els.tier.hidden !== !b) els.tier.hidden = !b
    const norm = !!p && normalizedOf(p)
    if (els.flag.hidden !== !norm) els.flag.hidden = !norm
    const ph = !!p && !tier
    setDom(els.ph, 'text', ph ? p.name : '')
    if (els.ph.hidden !== !ph) els.ph.hidden = !ph
  }

  /** 视图变化时记下时间，层级切换据此防抖 */
  function viewNow(): View {
    const v = store.get()
    if (v !== lastView) {
      if (lastView) lastMove = performance.now()
      lastView = v
    }
    return v
  }

  function draw() {
    if (disposed || !bw) return
    if (!visible) {
      // 滚出视口的格子不画，只让文字跟上
      naming(held ? held.pic() : own())
      return
    }
    const now = performance.now()
    const v = viewNow()
    if (cur) plan(cur, v, now)
    if (held) held.sync()
    const p = held ? held.pic() : own()
    const r = paint(p, v)
    label(p, r.tier, r.level)
    if (!held && p && p === cur && r.tier > p.painted) {
      p.painted = r.tier
      perf.mark(`cmp:paint:${p.id}:${TIERS[r.tier]}`)
    }
    if (r.defer) store.mark(self)
    const ok = !cur || !busy(cur)
    if (ok !== done) {
      done = ok
      if (ok) opts.onPrefetch?.()
    }
    // 原图下载中定时刷新进度
    const f = p?.full
    if (f && !f.entry && !f.error && !progTimer) progTimer = setTimeout(() => { progTimer = null; changed() }, 250)
  }

  function apply(w: number, h: number, r: number, pw: number, ph: number) {
    pending = null
    const pv = bw ? previewSize(cw, ch, dpr) : 0
    cw = w
    ch = h
    dpr = r
    // 优先用浏览器给出的设备像素尺寸：格子落在小数像素位置时画布与屏幕像素一一对应，不被再次插值
    bw = Math.max(1, pw || Math.round(w * r))
    bh = Math.max(1, ph || Math.round(h * r))
    els.canvas.width = bw
    els.canvas.height = bh
    if (cur) setPreview(cur)
    if (pv !== previewSize(cw, ch, dpr)) opts.onPrefetch?.()
    // 分配后立即重画，避免空白一帧；按住看本格的格子也要重画
    draw()
    for (const x of watchers) store.mark(x)
  }

  function settle() {
    if (settleTimer) clearTimeout(settleTimer)
    settleTimer = null
    if (pending) apply(...pending)
  }

  const self: CellRenderer = {
    setImage(img) {
      if (img && (img.i < 0 || img.i >= img.ix.n)) img = null
      if (img && cur && img.id === cur.id && img.ix === cur.ix && img.i === cur.i) return
      if (!img && !cur) return
      const old = cur
      cur = img ? newPic(img) : null
      if (old) {
        if (!stale && cur && has(old) && !has(cur)) {
          stale = old
          staleTimer = setTimeout(dropStale, STALE_MS)
        } else releasePic(old)
      }
      if (!cur || has(cur)) dropStale()
      changed()
    },
    setHold(other) {
      if (other === held || other === self) return
      held?.watch(self, false)
      held = other
      other?.watch(self, true)
      draw()
    },
    resize(w, h, r, pw = 0, ph = 0) {
      if (!(w > 0 && h > 0)) return
      if (!bw) { apply(w, h, r, pw, ph); return }
      if (w === cw && h === ch && r === dpr && (!pw || pw === bw) && (!ph || ph === bh)) {
        pending = null
        return
      }
      pending = [w, h, r, pw, ph]
      if (settleTimer) clearTimeout(settleTimer)
      settleTimer = setTimeout(settle, SETTLE_MS)
    },
    setVisible(v) {
      if (v === visible) return
      visible = v
      if (v) store.mark(self)
    },
    geom: () => (bw ? { cw, ch, dpr, pv: previewSize(cw, ch, dpr) } : null),
    relabel: () => changed(),
    draw,
    pic: own,
    settled: () => !visible || !cur || !busy(cur),
    sync() {
      // 可见的格子自己每帧都会规划
      if (visible || disposed || !bw || !cur) return
      plan(cur, viewNow(), performance.now())
    },
    watch(r, on) {
      if (on) watchers.add(r)
      else watchers.delete(r)
    },
    dispose() {
      if (disposed) return
      for (const w of [...watchers]) w.setHold(null)
      disposed = true
      held?.watch(self, false)
      held = null
      ro.disconnect()
      io.disconnect()
      document.removeEventListener('transitionend', onEnd)
      for (const t of [staleTimer, settleTimer, levelTimer, progTimer]) if (t) clearTimeout(t)
      unreg()
      if (cur) releasePic(cur)
      if (stale) releasePic(stale)
      cur = stale = null
    },
  }

  const unreg = store.add(self)
  const ro = new ResizeObserver((es) => {
    const e = es[es.length - 1], r = e.contentRect, d = e.devicePixelContentBoxSize?.[0]
    self.resize(r.width, r.height, window.devicePixelRatio || 1, d?.inlineSize, d?.blockSize)
  })
  try {
    ro.observe(els.vp, { box: 'device-pixel-content-box' })
  } catch {
    // 不支持设备像素尺寸的浏览器（Safari）按 CSS 尺寸 × dpr 取整
    ro.observe(els.vp)
  }
  const io = new IntersectionObserver((es) => self.setVisible(es[es.length - 1].isIntersecting))
  io.observe(els.vp)
  // 折叠栏动画结束时立即按最终尺寸重新分配
  const onEnd = (e: TransitionEvent) => { if (e.propertyName === 'flex-basis') settle() }
  document.addEventListener('transitionend', onEnd)
  const r0 = els.vp.getBoundingClientRect()
  self.resize(r0.width, r0.height, window.devicePixelRatio || 1)
  return self
}
