import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ImageEntry } from '../../api/types'
import { badgeOf, type Pic } from './CellRenderer.ts'
import { buildIndex } from './imageIndex.ts'
import { LoadError, type Handle } from './loader.ts'

const ix = buildIndex(1, 'e', { total: 1, missing: 0, skipped: 0, truncated: false, source: 'agent', files: [['a.png', 100, ''] as ImageEntry] })
const h = (o: Partial<Handle> = {}) => ({ entry: null, error: null, progress: 0, released: false, ...o }) as unknown as Handle
const done = h({ entry: {} as Handle['entry'] })
const pic = (o: Partial<Pic> = {}): Pic => ({
  id: 1, ix, i: 0, path: 'a.png', name: 'a.png', v: '', size: 100, thumb: null, preview: null, oldPreview: null, pv: 2048,
  full: null, tiles: new Map(), level: -1, noPreview: false, noTiles: false, onlyPreview: false, painted: 0, ...o,
})

test('角标描述实际画出的层', () => {
  assert.equal(badgeOf(pic({ size: -1 }), 0, -1), '文件不存在')
  assert.equal(badgeOf(pic({ thumb: done, preview: h() }), 1, -1), '加载中')
  assert.equal(badgeOf(pic({ thumb: h() }), 0, -1), '加载中')
  assert.equal(badgeOf(pic({ thumb: done, preview: done }), 2, -1), '预览')
  assert.equal(badgeOf(pic({ preview: done, onlyPreview: true }), 2, -1), '仅显示预览')
  assert.equal(badgeOf(pic(), 3, 0), '原始像素')
  assert.equal(badgeOf(pic(), 3, 2), '无损 1/4')
  // 原图下载中显示进度；预览生成不了时说明原因
  assert.equal(badgeOf(pic({ preview: done, full: h({ progress: 0.37 }) }), 2, -1), '原图 37%')
  assert.equal(badgeOf(pic({ noPreview: true, full: h({ progress: 0.5 }) }), 1, -1), '无法生成预览 · 原图 50%')
  // 预览失败时显示原因（含重试后仍失败的网络错误）；释放时的已取消不算
  assert.equal(badgeOf(pic({ thumb: done, preview: h({ error: new LoadError(415, '无法解码') }) }), 1, -1), '无法解码')
  assert.equal(badgeOf(pic({ thumb: done, preview: h({ error: new LoadError(0, '网络错误') }) }), 1, -1), '网络错误')
  assert.equal(badgeOf(pic({ thumb: done, preview: h({ error: new LoadError(0, '已取消'), released: true }) }), 1, -1), '缩略图')
})

// ---- 渲染器（DOM、观察器与 loader 用桩代替）
const g = globalThis as Record<string, unknown>
const ios = new Map<unknown, (es: { isIntersecting: boolean }[]) => void>()
g.document = { addEventListener() {}, removeEventListener() {} }
g.window = { devicePixelRatio: 1 }
g.ResizeObserver = class { observe() {} disconnect() {} }
g.IntersectionObserver = class {
  cb: (es: { isIntersecting: boolean }[]) => void
  constructor(cb: (es: { isIntersecting: boolean }[]) => void) { this.cb = cb }
  observe(el: unknown) { ios.set(el, this.cb) }
  disconnect() {}
}

const { createCellRenderer } = await import('./CellRenderer.ts')
const { loader } = await import('./loader.ts')
const { createViewStore } = await import('./viewStore.ts')
const { clampView } = await import('./geometry.ts')

/** 立即完成的假请求；pendingTile 为 true 的瓦片先挂起，finish() 时完成；failKind 的请求以网络错误失败 */
function fakeLoader(W: number, H: number, pendingTile: (l: number) => boolean = () => false, failKind = '') {
  const reqs: string[] = []
  const held: (() => void)[] = []
  const orig = loader.acquire
  loader.acquire = (url: string) => {
    reqs.push(url)
    const q = new URLSearchParams(url.split('?')[1])
    const kind = q.get('kind')
    const w = kind === 'preview' ? 1024 : kind === 'thumb' ? 256 : kind === 'tile' ? 512 : W
    const entry = { url, meta: { W, H, format: 'png', native: false, lossless: false, normalized: false, tile: 512 }, blob: null, bitmap: { url }, w, h: w, bytes: 1000, drawn: false }
    let ok!: (e: typeof entry) => void
    let no!: (e: LoadError) => void
    const h = {
      url, as: 'bitmap', promise: new Promise((r, j) => { ok = r; no = j }), entry: null as typeof entry | null, error: null as LoadError | null,
      progress: 0, released: false, setPrio() {}, release() {},
    }
    h.promise.catch(() => {})
    const done = () => { h.entry = entry; ok(entry) }
    if (kind === failKind) { h.error = new LoadError(0, '网络错误'); no(h.error) }
    else if (kind === 'tile' && pendingTile(Number(q.get('l')))) held.push(done)
    else done()
    return h as unknown as Handle
  }
  return { reqs, finish: () => held.splice(0).forEach((f) => f()), restore: () => { loader.acquire = orig } }
}

function cell(store: ReturnType<typeof createViewStore>, id: number, W: number, H: number, size = 50e6, dpr = 1, onPrefetch?: () => void) {
  const draws: [string, boolean][] = []
  /** 一帧里的清空与绘制：['clear' | url, 设备像素矩形, 插值质量] */
  const ops: [string, string, string][] = []
  const ctx = {
    imageSmoothingEnabled: true, imageSmoothingQuality: 'low', setTransform() {},
    clearRect(...r: number[]) {
      // 整个画布清空时开始新的一帧
      if (!r[0] && !r[1] && r[2] === canvas.width && r[3] === canvas.height) { draws.length = 0; ops.length = 0 }
      else ops.push(['clear', r.join(','), ''])
    },
    drawImage(b: { url: string }, ...r: number[]) {
      draws.push([b.url, ctx.imageSmoothingEnabled])
      ops.push([b.url, r.join(','), ctx.imageSmoothingQuality])
    },
  }
  const node = () => ({ hidden: false, textContent: '', attrs: {} as Record<string, string>, setAttribute(k: string, v: string) { this.attrs[k] = v } })
  const canvas = { ...node(), width: 0, height: 0, getContext: () => ctx }
  const vp = { getBoundingClientRect: () => ({ width: 400, height: 300 }) }
  ;(g.window as { devicePixelRatio: number }).devicePixelRatio = dpr
  const els = { vp, canvas, name: node(), tier: node(), flag: node(), ph: node() }
  const ix = buildIndex(id, 'e', { total: 2, missing: 0, skipped: 0, truncated: false, source: 'agent', files: [['a.tif', size, ''], ['b.tif', size, '']] })
  for (const i of [0, 1]) { ix.dims[2 * i] = W; ix.dims[2 * i + 1] = H }
  const r = createCellRenderer(els as unknown as Parameters<typeof createCellRenderer>[0], store, (x) => `set${x}`, { onPrefetch })
  r.setImage({ id, ix, i: 0 })
  return { r, els, draws, ops, ix, hide: () => ios.get(vp)!([{ isIntersecting: false }]) }
}
const sleep = (ms: number) => new Promise((ok) => setTimeout(ok, ms))
const kinds = (d: [string, boolean][]) => d.map(([u]) => { const q = new URLSearchParams(u.split('?')[1]); return q.get('kind') === 'tile' ? `l${q.get('l')}x${q.get('x')}y${q.get('y')}` : q.get('kind') })

test('按住对比：对方格子滚出视口时也按当前视图补齐瓦片', async () => {
  const W = 7680, H = 5760
  const f = fakeLoader(W, H)
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const A = cell(store, 1, W, H), G = cell(store, 7, W, H)
    store.flush()
    store.set(clampView(16, 0, 0, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    G.hide()
    store.set(clampView(16, -3000, -2000, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    const A_tiles = kinds(A.draws).filter((k) => k!.startsWith('l'))
    A.r.setHold(G.r)
    assert.equal(A.els.canvas.attrs['aria-label'], 'set7 a.tif')
    assert.equal(A.els.canvas.attrs['data-tier'], 'lossless')
    // 画的是对方在当前视图下的同一组瓦片
    assert.deepEqual(kinds(A.draws).filter((k) => k!.startsWith('l')), A_tiles)
    A.r.setHold(null)
    assert.equal(A.els.canvas.attrs['aria-label'], 'set1 a.tif')
  } finally {
    f.restore()
  }
})

test('滚出视口的格子不画，但文件名与 aria-label 跟着换', () => {
  const f = fakeLoader(3840, 2160)
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const c = cell(store, 1, 3840, 2160)
    store.flush()
    c.hide()
    c.r.setImage({ id: 1, ix: c.ix, i: 1 })
    store.flush()
    assert.equal(c.els.name.textContent, 'b.tif')
    assert.equal(c.els.canvas.attrs['aria-label'], 'set1 b.tif')
  } finally {
    f.restore()
  }
})

test('只有原图像素（第 0 层）放大时关插值；垫底的粗层照常插值', async () => {
  const W = 2000, H = 1500
  let hold0 = true
  const f = fakeLoader(W, H, (l) => l === 0 && hold0)
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const c = cell(store, 1, W, H)
    store.flush()
    // f = 0.2：z=2 时 k=0.4，用第 1 层
    store.set(clampView(2, -200, -150, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    assert.equal(c.els.canvas.attrs['data-level'], '1')
    assert.ok(c.draws.filter(([u]) => u.includes('kind=tile')).every(([, smooth]) => smooth))
    // z=8 时 k=1.6：第 0 层还没到，第 1 层放大垫底，必须插值
    store.set(clampView(8, -1400, -1050, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    const l1 = c.draws.filter(([u]) => u.includes('&l=1&'))
    assert.ok(l1.length && l1.every(([, smooth]) => smooth))
    assert.equal(c.els.canvas.attrs['data-tier'], 'preview')
    // 第 0 层到了：原图像素不插值
    hold0 = false
    f.finish()
    await sleep(0)
    store.flush()
    const l0 = c.draws.filter(([u]) => u.includes('&l=0&'))
    assert.ok(l0.length && l0.every(([, smooth]) => !smooth))
    assert.equal(c.els.canvas.attrs['data-tier'], 'lossless')
    assert.equal(c.els.canvas.attrs['data-level'], '0')
  } finally {
    f.restore()
  }
})

test('当前图的视口瓦片都到了才算就绪，就绪时回调重算预取', async () => {
  const W = 2000, H = 1500
  const f = fakeLoader(W, H, (l) => l === 0)
  let calls = 0
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const c = cell(store, 1, W, H, 50e6, 1, () => calls++)
    store.flush()
    assert.ok(c.r.settled())
    const before = calls
    store.set(clampView(8, -1400, -1050, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    // 第 0 层瓦片还在路上
    assert.ok(!c.r.settled())
    f.finish()
    await sleep(0)
    store.flush()
    assert.ok(c.r.settled())
    assert.equal(calls, before + 1)
  } finally {
    f.restore()
  }
})

test('无损层先清空所在矩形再画（带透明度的图不与预览叠加），插值质量为 medium', async () => {
  const W = 2000, H = 1500
  const f = fakeLoader(W, H)
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const c = cell(store, 1, W, H)
    store.flush()
    store.set(clampView(8, -1400, -1050, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    assert.equal(c.els.canvas.attrs['data-tier'], 'lossless')
    const tiles = c.ops.filter(([u]) => u.includes('kind=tile'))
    assert.ok(tiles.length)
    for (const t of tiles) {
      const i = c.ops.indexOf(t)
      assert.deepEqual(c.ops[i - 1].slice(0, 2), ['clear', t[1]])
    }
    // 垫底的预览不清空
    const pv = c.ops.findIndex(([u]) => u.includes('kind=preview'))
    assert.ok(pv === 0 && c.ops.every(([, , q], i) => i === 0 || q === '' || q === 'medium'))
    assert.equal(c.ops[0][2], 'medium')
  } finally {
    f.restore()
  }
})

test('缩小手势中立即换到更粗的层，不按旧的细层请求新露出的瓦片', async () => {
  const W = 7680, H = 4320
  const f = fakeLoader(W, H)
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    cell(store, 1, W, H)
    store.flush()
    // f = 400 / 7680：z=10 时 k≈0.52，用第 0 层；缩到 z=2 时用第 3 层
    store.set(clampView(10, -1800, -1300, 400, 300))
    store.flush()
    await sleep(80)
    store.flush()
    const before = new Set(f.reqs.filter((u) => u.includes('&l=0&'))).size
    assert.ok(before > 0)
    // 一次手势里每帧缩小一点，帧间隔远小于防抖时间
    let v = store.get()
    for (let z = 10; z > 2; z /= 1.05) {
      v = clampView(z, v.x * (z / v.z) + 200 * (1 - z / v.z), v.y * (z / v.z) + 150 * (1 - z / v.z), 400, 300)
      store.set(v)
      store.flush()
    }
    const after = new Set(f.reqs.filter((u) => u.includes('&l=0&'))).size
    assert.ok(after - before <= 4, `${after - before} 块第 0 层瓦片`)
  } finally {
    f.restore()
  }
})

test('网络错误（重试后仍失败）时重画并显示原因', async () => {
  const f = fakeLoader(3840, 2160, () => false, 'preview')
  try {
    const store = createViewStore(undefined, () => 1, () => {})
    const c = cell(store, 1, 3840, 2160)
    store.flush()
    await sleep(0)
    store.flush()
    assert.equal(c.els.canvas.attrs['data-tier'], 'thumb')
    assert.equal(c.els.tier.textContent, '网络错误')
  } finally {
    f.restore()
  }
})
