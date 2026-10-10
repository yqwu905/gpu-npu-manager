import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createLRU } from './bitmapCache.ts'
import { LIMIT_H1, LoadError, PRIO, createLoader, ranked, type Entry, type Limits, type Meta } from './loader.ts'

const tick = () => new Promise((r) => setImmediate(r))
const url = (lane: string, n: number | string, kind = 'thumb') => `/api/results/1/image?path=${lane}${n}&kind=${kind}`

interface Call { url: string; init: RequestInit; resolve(r: Response): void; reject(e: unknown): void; done: boolean }

/** 假 fetch（中止时拒绝）+ 假时钟 + 假解码 */
function harness(limits: Limits = LIMIT_H1) {
  const calls: Call[] = []
  const fetch = (u: string, init: RequestInit) =>
    new Promise<Response>((resolve, reject) => {
      const c: Call = { url: u, init, resolve, reject, done: false }
      calls.push(c)
      init.signal!.addEventListener('abort', () => { c.done = true; reject(new DOMException('aborted', 'AbortError')) })
    })
  let t = 0, tid = 0
  const timers: { at: number; fn: () => void; id: number }[] = []
  const clock = {
    now: () => t,
    setTimeout: (fn: () => void, ms: number) => { timers.push({ at: t + ms, fn, id: ++tid }); return tid },
    clearTimeout: (id: unknown) => { const i = timers.findIndex((x) => x.id === id); if (i >= 0) timers.splice(i, 1) },
    async advance(ms: number) {
      const end = t + ms
      for (;;) {
        timers.sort((a, b) => a.at - b.at)
        const n = timers[0]
        if (!n || n.at > end) break
        timers.shift()
        t = n.at
        n.fn()
        await tick()
      }
      t = end
      await tick()
    },
  }
  let decodes = 0
  const disposed: string[] = []
  const cache = createLRU<Entry>(1e12, (e) => disposed.push(e.url))
  const loader = createLoader({
    fetch, mock: () => false, limits, cache, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    decode: async () => { decodes++; return { width: 4, height: 2, close() {} } as unknown as ImageBitmap },
  })
  const live = () => calls.filter((c) => !c.done)
  async function respond(u: string, status = 200, headers: Record<string, string> = {}, body = 'xx') {
    await tick()
    const c = live().find((x) => x.url === u)
    assert.ok(c, `no live request for ${u}`)
    c.done = true
    c.resolve(new Response(status === 200 ? body : JSON.stringify({ detail: '错误' }), { status, headers }))
    await tick()
    await tick()
  }
  return { loader, calls, live, clock, respond, cache, disposed, decodes: () => decodes }
}

test('车道上限与总上限不被突破，非主车道至少给主车道留一个', async () => {
  const { loader, calls, live, respond } = harness()
  const hs = [
    ...Array.from({ length: 12 }, (_, i) => loader.acquire(url('thumb', i), 'thumb', 3 + (i % 3), 'blob')),
    ...Array.from({ length: 8 }, (_, i) => loader.acquire(url('prefetch', i, 'preview'), 'prefetch', 2 + (i % 3), 'bitmap')),
    ...Array.from({ length: 8 }, (_, i) => loader.acquire(url('main', i, 'tile'), 'main', i % 2, 'bitmap')),
  ]
  let r = 7
  await tick()
  for (let guard = 0; guard < 200 && live().length; guard++) {
    await tick()
    const by = (lane: string) => live().filter((c) => c.url.includes(`path=${lane}`)).length
    assert.ok(by('thumb') <= 3 && by('prefetch') <= 2 && live().length <= 5 && by('thumb') + by('prefetch') <= 4)
    r = (r * 31 + 11) % 97
    const c = live()[r % live().length]
    await respond(c.url)
  }
  assert.equal(calls.length, 28)
  assert.equal(new Set(calls.map((c) => c.url)).size, 28)
  for (const h of hs) assert.ok(h.entry)
  assert.equal(loader.stats().inflight.total, 0)
})

test('按优先级先后发出，setPrio 会调整排队顺序', async () => {
  const { loader, calls, respond } = harness({ total: 1, thumb: 1, prefetch: 1 })
  loader.acquire(url('a', 0), 'main', 3, 'blob')
  loader.acquire(url('b', 0), 'main', 1, 'blob')
  loader.acquire(url('c', 0), 'main', 2, 'blob')
  await tick()
  assert.deepEqual(calls.map((c) => c.url), [url('b', 0)])
  await respond(url('b', 0))
  await respond(url('c', 0))
  assert.deepEqual(calls.map((c) => c.url), [url('b', 0), url('c', 0), url('a', 0)])

  const x = harness({ total: 1, thumb: 1, prefetch: 1 })
  x.loader.acquire(url('x', 0), 'main', 0, 'blob')
  const a = x.loader.acquire(url('a', 0), 'main', 5, 'blob')
  x.loader.acquire(url('b', 0), 'main', 4, 'blob')
  await tick()
  a.setPrio(1)
  await x.respond(url('x', 0))
  assert.equal(x.calls[1].url, url('a', 0))
  // 第一个请求用 high，其余 low
  assert.equal((x.calls[0].init as RequestInit & { priority?: string }).priority, 'high')
  assert.equal((x.calls[1].init as RequestInit & { priority?: string }).priority, 'low')
})

test('眼前的图（P0、P1）没取完时 P3 及以后的不发，+1 预取（P2）照发', async () => {
  const { loader, live, respond } = harness()
  loader.acquire(url('m', 0, 'tile'), 'main', PRIO.lossless, 'bitmap')
  loader.acquire(url('t', 0), 'thumb', PRIO.thumbs, 'blob')
  loader.acquire(url('n', 0, 'preview'), 'prefetch', PRIO.near, 'bitmap')
  loader.acquire(url('r', 0, 'tile'), 'main', PRIO.near, 'bitmap')
  loader.acquire(url('x', 0, 'preview'), 'prefetch', PRIO.next, 'bitmap')
  await tick()
  assert.deepEqual(live().map((c) => c.url), [url('m', 0, 'tile'), url('x', 0, 'preview')])
  await respond(url('m', 0, 'tile'))
  assert.deepEqual(live().map((c) => c.url).sort(), [url('x', 0, 'preview'), url('t', 0), url('n', 0, 'preview'), url('r', 0, 'tile')].sort())
})

test('缩略图栏显示着的缩略图：位图句柄同步得到（当帧可画占位），不请求不解码；同步不可用时解码 Blob', async () => {
  let decodes = 0
  const meta = { W: 3840, H: 2160, format: 'png', native: true, lossless: false, normalized: false, tile: 0 }
  const shownImg = { naturalWidth: 256, naturalHeight: 144 } as unknown as HTMLImageElement
  const loader = createLoader({
    mock: () => false, cache: createLRU<Entry>(1e12, () => {}), limits: LIMIT_H1,
    fetch: () => { throw new Error('不应请求') },
    blobOf: (u) => ({ blob: new Blob(['x']), meta, src: u.includes('path=a') ? 'blob:a' : 'blob:b' }),
    syncImage: (src) => (src === 'blob:a' ? shownImg : null),
    decode: async () => { decodes++; return { width: 256, height: 144, close() {} } as unknown as ImageBitmap },
  })
  const h = loader.acquire(url('a', 0), 'main', PRIO.current, 'bitmap')
  assert.equal(h.entry?.bitmap, shownImg)
  assert.deepEqual([h.entry!.w, h.entry!.h, h.entry!.meta.W], [256, 144, 3840])
  const b = loader.acquire(url('b', 0), 'main', PRIO.current, 'bitmap')
  assert.equal(b.entry, null)
  await b.promise
  assert.equal(decodes, 1)
})

test('解码并发：小图最多 4 个，缩略图另算（6 个格子的占位同时解码）', async () => {
  const pending: (() => void)[] = []
  const max = { thumb: 0, other: 0 }, cur = { thumb: 0, other: 0 }
  const blob = (type: string) => ({ blob: new Blob(['x'], { type }), meta: { W: 0, H: 0, format: 'png', native: true, lossless: false, normalized: false, tile: 0 } })
  const loader = createLoader({
    mock: () => false, cache: createLRU<Entry>(1e12, () => {}), limits: LIMIT_H1,
    fetch: () => new Promise(() => {}),
    // 缩略图栏已有 Blob：不请求直接解码；解码由测试逐个放行
    blobOf: (u) => blob(u.includes('kind=thumb') ? 'image/jpeg' : 'image/png'),
    decode: (b) => new Promise((ok) => {
      const k = b.type === 'image/jpeg' ? 'thumb' : 'other'
      max[k] = Math.max(max[k], ++cur[k])
      pending.push(() => { cur[k]--; ok({ width: 4, height: 2, close() {} } as unknown as ImageBitmap) })
    }),
  })
  for (let c = 0; c < 6; c++) loader.acquire(url(`c${c}`, 0), 'main', PRIO.current, 'bitmap')
  for (let c = 0; c < 6; c++) loader.acquire(url(`p${c}`, 0, 'tile'), 'main', PRIO.lossless, 'bitmap')
  for (let k = 0; k < 20; k++) await tick()
  assert.equal(max.thumb, 6)
  assert.equal(max.other, 4)
  while (pending.length) { pending.shift()!(); for (let k = 0; k < 3; k++) await tick() }
  assert.equal(max.other, 4)
})

test('ranked：同一优先级内各格子按名次交错，5 个连接落在 5 张不同的图上；小数不越级', async () => {
  const { loader, calls, live } = harness()
  // 6 个格子依次请求各自的 4 块瓦片（P1），先请求的格子 seq 小
  for (let c = 0; c < 6; c++) for (let k = 0; k < 4; k++) loader.acquire(url(`c${c}t`, k, 'tile'), 'main', ranked(PRIO.lossless, k), 'bitmap')
  await tick()
  assert.deepEqual(live().map((x) => x.url), [0, 1, 2, 3, 4].map((c) => url(`c${c}t`, 0, 'tile')))
  assert.ok(ranked(PRIO.current, 1) < PRIO.lossless && ranked(PRIO.lossless, 500) < PRIO.next)
  assert.equal((calls[0].init as RequestInit & { priority?: string }).priority, 'low')
  const y = harness()
  y.loader.acquire(url('p', 0, 'preview'), 'main', ranked(PRIO.current, 1), 'bitmap')
  await tick()
  assert.equal((y.calls[0].init as RequestInit & { priority?: string }).priority, 'high')
})

test('同一 URL 只请求一次；解码结果进缓存，再次 acquire 同步可得', async () => {
  const { loader, calls, respond, cache, decodes } = harness()
  const u = url('p', 1, 'preview')
  const h1 = loader.acquire(u, 'main', 0, 'bitmap')
  const h2 = loader.acquire(u, 'prefetch', 4, 'bitmap')
  const h3 = loader.acquire(u, 'thumb', 3, 'blob')
  await respond(u, 200, { 'X-Image-Width': '3840', 'X-Image-Height': '2160', 'X-Image-Format': 'png', 'X-Image-Native': '1', 'X-Lossless': '0' })
  const [e1, e2, e3] = await Promise.all([h1.promise, h2.promise, h3.promise])
  assert.equal(calls.length, 1)
  assert.equal(decodes(), 1)
  assert.equal(e1, e2)
  assert.ok(e1.bitmap && !e1.blob && e1.bytes === 4 * 2 * 4)
  assert.ok(e3.blob && !e3.bitmap)
  assert.deepEqual(e1.meta, { W: 3840, H: 2160, format: 'png', native: true, lossless: false, normalized: false, tile: 0 })
  // 持有句柄期间 pin 住
  assert.ok(cache.isPinned(u))
  const h4 = loader.acquire(u, 'main', 0, 'bitmap')
  assert.equal(h4.entry, e1)
  assert.equal(calls.length, 1)
  for (const h of [h1, h2, h4]) h.release()
  assert.ok(!cache.isPinned(u))
})

test('release：排队的丢弃、进行中的中止；还有别的引用时不中止', async () => {
  const { loader, calls, live, respond } = harness({ total: 1, thumb: 1, prefetch: 1 })
  const a = loader.acquire(url('a', 0), 'main', 0, 'blob')
  const a2 = loader.acquire(url('a', 0), 'main', 1, 'blob')
  const b = loader.acquire(url('b', 0), 'main', 1, 'blob')
  await tick()
  b.release()
  a2.release()
  await tick()
  assert.equal(live().length, 1)
  await assert.rejects(a2.promise, (e) => e instanceof LoadError && e.status === 0)
  const c = loader.acquire(url('c', 0), 'main', 1, 'blob')
  a.release()
  await tick()
  await tick()
  assert.ok(calls[0].done && (calls[0].init.signal as AbortSignal).aborted)
  assert.deepEqual(live().map((x) => x.url), [url('c', 0)])
  await respond(url('c', 0))
  assert.ok(c.entry)
  assert.ok(!calls.some((x) => x.url === url('b', 0)))
  assert.equal(loader.stats().jobs, 0)
})

test('503 按 Retry-After 与 1、2、4、8 秒退避，最多 5 次', async () => {
  const { loader, calls, respond, clock } = harness()
  const u = url('busy', 0)
  const h = loader.acquire(u, 'main', 0, 'blob')
  await respond(u, 503, { 'Retry-After': '3' })
  await clock.advance(2999)
  assert.equal(calls.length, 1)
  await clock.advance(1)
  assert.equal(calls.length, 2)
  for (const wait of [2000, 4000, 8000]) {
    await respond(u, 503)
    await clock.advance(wait - 1)
    assert.equal(calls.filter((c) => !c.done).length, 0)
    await clock.advance(1)
  }
  assert.equal(calls.length, 5)
  await respond(u, 503)
  await assert.rejects(h.promise, (e) => e instanceof LoadError && e.status === 503 && e.message === '服务繁忙')
  assert.equal(loader.stats().jobs, 0)
  // 503 不进失败缓存
  loader.acquire(u, 'main', 0, 'blob')
  await tick()
  assert.equal(calls.length, 6)
})

test('网络错误（连接断开、响应体读到一半）按 1、2、4、8 秒退避重试，最后以 status 0 失败', async () => {
  const { loader, calls, live, clock } = harness()
  const u = url('net', 0, 'preview')
  const h = loader.acquire(u, 'main', 0, 'bitmap')
  const cut = async () => {
    await tick()
    const c = live().find((x) => x.url === u)!
    c.done = true
    c.reject(new TypeError('network error'))
    await tick()
    await tick()
  }
  await cut()
  assert.ok(!h.error)
  for (const wait of [1000, 2000, 4000, 8000]) {
    await clock.advance(wait - 1)
    assert.equal(live().length, 0)
    await clock.advance(1)
    assert.equal(live().length, 1)
    if (wait < 8000) await cut()
  }
  // 第 5 次成功
  const c = live()[0]
  c.done = true
  c.resolve(new Response('xx', { status: 200 }))
  await h.promise
  assert.equal(calls.length, 5)
  const h2 = loader.acquire(url('net', 1, 'preview'), 'main', 0, 'bitmap')
  for (let i = 0; i < 5; i++) {
    await tick()
    const x = live().find((y) => y.url === h2.url)!
    x.done = true
    x.reject(new TypeError('network error'))
    await tick()
    await clock.advance(10_000)
  }
  await assert.rejects(h2.promise, (e) => e instanceof LoadError && e.status === 0 && e.message === '网络错误')
  assert.ok(!h2.released)
})

test('404/413/415 缓存 30 秒', async () => {
  const { loader, calls, respond, clock } = harness()
  for (const [status, text] of [[404, '文件不存在'], [413, '图片过大'], [415, '无法解码']] as [number, string][]) {
    const u = url('bad', status)
    const h = loader.acquire(u, 'main', 0, 'bitmap')
    await respond(u, status)
    await assert.rejects(h.promise)
    assert.equal(h.error?.message, text)
    const again = loader.acquire(u, 'main', 0, 'bitmap')
    assert.equal(again.error?.status, status)
    again.release()
  }
  assert.equal(calls.length, 3)
  await clock.advance(30_001)
  loader.acquire(url('bad', 404), 'main', 0, 'bitmap')
  await tick()
  assert.equal(calls.length, 4)
  // 其他错误不缓存
  const u = url('err', 502)
  loader.acquire(u, 'main', 0, 'blob')
  await respond(u, 502)
  loader.acquire(u, 'main', 0, 'blob')
  await tick()
  assert.equal(calls.filter((c) => c.url === u).length, 2)
})

test('连按方向键时预览推迟到最后一次之后 100 ms，缩略图不推迟', async () => {
  const { loader, calls, clock } = harness()
  loader.noteStep()
  await clock.advance(50)
  loader.noteStep()
  const p1 = loader.acquire(url('p', 1, 'preview'), 'main', 0, 'bitmap')
  loader.acquire(url('t', 1), 'main', 0, 'bitmap')
  await tick()
  assert.deepEqual(calls.map((c) => c.url), [url('t', 1)])
  // 又按了一次：上一张的预览没发就被放弃
  await clock.advance(50)
  loader.noteStep()
  p1.release()
  loader.acquire(url('p', 2, 'preview'), 'main', 0, 'bitmap')
  loader.acquire(url('p', 3, 'preview'), 'prefetch', 2, 'bitmap')
  await clock.advance(99)
  assert.equal(calls.length, 1)
  await clock.advance(1)
  assert.deepEqual(calls.map((c) => c.url), [url('t', 1), url('p', 2, 'preview'), url('p', 3, 'preview')])
  // 间隔超过 120 ms 不算连按
  await clock.advance(500)
  loader.noteStep()
  loader.acquire(url('p', 4, 'preview'), 'main', 0, 'bitmap')
  await tick()
  assert.equal(calls.length, 4)
  // 放大时的瓦片与原图同样推迟，被跳过的序号释放后不再请求
  loader.noteStep()
  await clock.advance(30)
  loader.noteStep()
  const skipped = loader.acquire(url('l', 5, 'tile'), 'main', 1, 'bitmap')
  loader.acquire(url('f', 5, 'full'), 'main', 1, 'bitmap')
  await tick()
  assert.equal(calls.length, 4)
  skipped.release()
  await clock.advance(100)
  assert.deepEqual(calls.slice(4).map((c) => c.url), [url('f', 5, 'full')])
})

test('meta 回调、full 的进度、abortAll', async () => {
  const { loader, live, respond } = harness()
  const seen: [string, Meta][] = []
  const off = loader.onMeta((u, m) => seen.push([u, m]))
  const u = url('f', 1, 'full')
  const h = loader.acquire(u, 'main', 1, 'bitmap')
  assert.equal(h.progress, 0)
  await respond(u, 200, { 'Content-Length': '6', 'X-Image-Format': 'png', 'X-Image-Native': '1', 'X-Lossless': '1', 'X-Normalized': '1' }, 'abcdef')
  await h.promise
  assert.equal(h.progress, 1)
  // 原图响应没带尺寸，用解码结果补上并再通知一次
  assert.deepEqual(seen.map(([x, m]) => [x, m.W, m.H, m.normalized]), [[u, 0, 0, true], [u, 4, 2, true]])
  off()
  // 缩略图先发出去（之后再有 P0 时 P3 不再新发，但已发出的照常进行）
  const a = loader.acquire(url('x', 1), 'thumb', 3, 'blob')
  await tick()
  const b = loader.acquire(url('y', 1), 'main', 0, 'bitmap')
  await tick()
  assert.equal(live().length, 2)
  loader.abortAll()
  await tick()
  assert.equal(live().length, 0)
  await assert.rejects(a.promise)
  await assert.rejects(b.promise)
  assert.equal(loader.stats().jobs, 0)
  assert.equal(loader.stats().inflight.total, 0)
})

test('位图句柄命中已缓存的 Blob 时不发请求，直接解码', async () => {
  const meta: Meta = { W: 40, H: 20, format: 'png', native: true, lossless: false, normalized: false, tile: 0 }
  const { loader, calls, decodes } = harness()
  loader.setBlobSource((u) => (u === url('t', 1) ? { blob: new Blob(['x']), meta } : null))
  const h = loader.acquire(url('t', 1), 'main', 0, 'bitmap')
  const e = await h.promise
  assert.equal(calls.length, 0)
  assert.equal(decodes(), 1)
  assert.equal(e.meta.W, 40)
  // 没有 Blob 的照常请求；释放后排队的不再解码
  loader.acquire(url('t', 2), 'main', 0, 'bitmap')
  const gone = loader.acquire(url('t', 1) + '&x=1', 'main', 0, 'bitmap')
  gone.release()
  await tick()
  assert.equal(calls.length, 1)
})

test('abortAll 时正在解码的结果不进缓存，同一 URL 之后重新请求', async () => {
  const pend: (() => void)[] = []
  let closed = 0
  const cache = createLRU<Entry>(1e12, () => {})
  const calls: string[] = []
  const loader = createLoader({
    mock: () => false, limits: LIMIT_H1, cache,
    fetch: async (u) => { calls.push(u); return new Response('xx', { status: 200, headers: { 'X-Image-Width': '7680', 'X-Image-Height': '4320' } }) },
    decode: () => new Promise((ok) => pend.push(() => ok({ width: 7680, height: 4320, close() { closed++ } } as unknown as ImageBitmap))),
  })
  const u = url('d', 1, 'full')
  const h = loader.acquire(u, 'main', 1, 'bitmap')
  for (let i = 0; i < 10 && !pend.length; i++) await tick()
  assert.equal(pend.length, 1)
  // 与 ComparePage 卸载时的顺序相同
  loader.abortAll()
  cache.clear()
  h.release()
  await assert.rejects(h.promise)
  pend[0]()
  for (let i = 0; i < 5; i++) await tick()
  assert.equal(cache.size, 0)
  assert.equal(closed, 1)
  assert.equal(loader.stats().jobs, 0)
  const again = loader.acquire(u, 'main', 1, 'bitmap')
  for (let i = 0; i < 10 && pend.length < 2; i++) await tick()
  assert.equal(calls.length, 2)
  pend[1]()
  assert.equal((await again.promise).w, 7680)
})

test('示例数据：解码中途加入的 blob 句柄拿到 Blob（PNG 编码比解码慢时也一样）', async () => {
  const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))
  let encodes = 0
  const loader = createLoader({
    mock: () => true, limits: LIMIT_H1, cache: createLRU<Entry>(1e12, () => {}),
    decodeRaw: async (r) => { await wait(10); return { width: r.w, height: r.h, close() {} } as unknown as ImageBitmap },
    encodeRaw: async () => { encodes++; await wait(40); return new Blob([new Uint8Array(4)], { type: 'image/png' }) },
  })
  const u = '/api/results/1/image?path=images%2F0001.png&kind=thumb'
  const bm = loader.acquire(u, 'main', 0, 'bitmap')
  for (let i = 0; i < 50 && loader.stats().decoding === 0; i++) await wait(5)
  assert.equal(loader.stats().decoding, 1)
  const b1 = loader.acquire(u, 'thumb', 3, 'blob')
  const b2 = loader.acquire(u, 'thumb', 3, 'blob')
  const [e, e1, e2] = await Promise.all([bm.promise, b1.promise, b2.promise])
  assert.ok(e.bitmap && !e.blob)
  assert.ok(e1.blob && !e1.bitmap && e2.blob)
  assert.equal(encodes, 1)
})
