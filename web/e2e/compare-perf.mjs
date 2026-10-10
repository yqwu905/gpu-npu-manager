// 对比页性能测试（计划 §8.2 的 S1–S8，S3 含连按的 S3b；S10 为无 Pillow 的 Agent 再跑 S2、S3、S5）：驱动真实的中心服务 + Agent，
// 读取 ?perf=1 的计数、Long Tasks、Event Timing、rAF 帧间隔与请求日志，按门槛判定，结果写到 $OUT_DIR/perf-<tag>.json。
// 不依赖项目里的 npm 包，使用全局安装的 playwright 与 /opt/pw-browsers 里的浏览器（不要 playwright install）：
//   python scripts/make_compare_dataset.py --out DIR …            # 6 组 set0..set5；S8 另需 6 组 8K（名称 k8-0..k8-5）
//   python scripts/bench_stack.py --data DIR --build [--no-pillow]
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node e2e/compare-perf.mjs --data DIR [--only S1,S2] [--runs 3] [--tag pillow]
import { execSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

process.env.PLAYWRIGHT_BROWSERS_PATH ??= '/opt/pw-browsers'
const require = createRequire(import.meta.url)
const { chromium } = require(join(execSync('npm root -g').toString().trim(), 'playwright'))

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d }
const DATA = arg('data')
if (!DATA) throw new Error('需要 --data DIR（bench_stack.py 写出的 ids.json 所在目录）')
const INFO = JSON.parse(readFileSync(join(DATA, 'ids.json'), 'utf8'))
const URL0 = arg('url', INFO.url)
const ONLY = arg('only', 'S7c,S1,S2,S3,S4,S5,S6,S7,S8').split(',')
const RUNS = Number(arg('runs', 3))
const TAG = arg('tag', INFO.no_pillow ? 'no-pillow' : 'pillow')
const OUT = process.env.OUT_DIR ?? tmpdir()
const VIEW = { width: 1920, height: 1080 }

// ---------------------------------------------------------------- 数据
const sets = await (await fetch(`${URL0}/api/results`)).json()
const idOf = (name) => sets.find((r) => r.name === name)?.id
const MAIN = ['set0', 'set1', 'set2', 'set3', 'set4', 'set5'].map(idOf)
const K8 = ['k8-0', 'k8-1', 'k8-2', 'k8-3', 'k8-4', 'k8-5'].map(idOf)
const lists = {}
async function list(id) {
  return (lists[id] ??= (await (await fetch(`${URL0}/api/results/${id}/images`)).json()).files)
}
/** 图案图的种子 = 组号（make_compare_dataset.py） */
const seedOf = (id) => MAIN.includes(id) ? MAIN.indexOf(id) : K8.indexOf(id)
const idxOf = (u) => Number(/img_(\d{5})/.exec(decodeURIComponent(u))?.[1] ?? -1)
const kindOf = (u) => /[?&]kind=(\w+)/.exec(u)?.[1] ?? ''
/** 与 resultsApi.imageUrl 相同的规范 URL */
const imageUrl = (id, [path, , v], kind, size) =>
  `/api/results/${id}/image?path=${encodeURIComponent(path)}${v ? `&v=${v}` : ''}&kind=${kind}${kind === 'preview' ? `&size=${size}` : ''}`
/** 预热服务端缓存：各结果集 [from, to) 的缩略图与预览（并发 6） */
async function warmUp(ids, from, to, size) {
  const urls = []
  for (const id of ids) {
    const f = await list(id)
    for (let i = from; i < Math.min(to, f.length); i++) if (f[i][1] !== -1) urls.push(imageUrl(id, f[i], 'preview', size), imageUrl(id, f[i], 'thumb'))
  }
  let next = 0
  const t = Date.now()
  await Promise.all(Array.from({ length: 6 }, async () => { while (next < urls.length) await (await fetch(URL0 + urls[next++])).arrayBuffer() }))
  console.log(`  预热 ${urls.length} 张（${((Date.now() - t) / 1000).toFixed(1)} s）`)
}
/** 页面实际用的预览档位 */
const previewSizeOf = (s) => Number(/[?&]size=(\d+)/.exec(s.reqs.find((r) => kindOf(r.u) === 'preview')?.u ?? '')?.[1] ?? 2048)

// 改版前的 Alt+单击算法：编辑距离最小，相同取序号小的
function editDistance(a, b) {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const row = [i]
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    prev = row
  }
  return prev[b.length]
}
const baseName = (p) => p.slice(Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')) + 1)
function naive(q, files) {
  let best = 0, bd = Infinity
  files.forEach((f, k) => { const d = editDistance(q, baseName(f[0])); if (d < bd) { bd = d; best = k } })
  return best
}

// ---------------------------------------------------------------- 统计
const pct = (a, q) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.ceil(q * s.length) - 1)] }
const med = (a) => pct(a, 0.5)
const r1 = (x) => (Number.isFinite(x) ? Math.round(x * 10) / 10 : x)
const results = {}
const gates = []
function gate(sc, name, value, limit, op = '<=') {
  const pass = op === '<=' ? value <= limit : op === '==' ? value === limit : value >= limit
  gates.push({ sc, name, value: r1(value), limit: `${op} ${limit}`, pass })
  console.log(`  ${pass ? 'PASS' : 'FAIL'} ${sc} ${name}: ${r1(value)} (${op} ${limit})`)
}

// ---------------------------------------------------------------- 页面
const browser = await chromium.launch({ args: ['--force-color-profile=srgb'] })
const cdp = await browser.newBrowserCDPSession()
const rssKb = (pid) => { try { return Number(/VmRSS:\s+(\d+)/.exec(readFileSync(`/proc/${pid}/status`, 'utf8'))[1]) } catch { return 0 } }
/** 最大的渲染进程与 GPU 进程的 RSS（MB） */
async function rss() {
  const { processInfo } = await cdp.send('SystemInfo.getProcessInfo')
  let renderer = 0, gpu = 0
  for (const p of processInfo) {
    if (p.type === 'renderer') renderer = Math.max(renderer, rssKb(p.id))
    else if (p.type === 'GPU') gpu += rssKb(p.id)
  }
  return { renderer: renderer / 1024, gpu: gpu / 1024 }
}

// 页面里的采集器：Long Tasks、Event Timing、键盘 / 指针时间、按住后的下一帧、rAF 帧间隔（可顺带采样一个最大值）
function collectors() {
  const m = (window.__m = { lt: [], ev: [], keys: [], clicks: [], holds: [], status: [], fr: null })
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) m.lt.push([e.startTime, e.duration]) }).observe({ type: 'longtask', buffered: true }) } catch {}
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) m.ev.push([e.name, e.startTime, e.duration]) }).observe({ type: 'event', durationThreshold: 16, buffered: true }) } catch {}
  addEventListener('keydown', (e) => m.keys.push(e.timeStamp), true)
  addEventListener('click', (e) => m.clicks.push(e.timeStamp), true)
  addEventListener('pointerdown', (e) => {
    if (!e.target.closest?.('.cmp-hold')) return
    const t0 = e.timeStamp
    requestAnimationFrame((t1) => m.holds.push([t0, t1]))
  }, true)
  m.startFrames = (sel) => {
    const st = (m.fr = { d: [], max: 0, on: true, last: 0 })
    const tick = (t) => {
      if (!st.on) return
      if (st.last) st.d.push(t - st.last)
      st.last = t
      if (sel) st.max = Math.max(st.max, document.querySelectorAll(sel).length)
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  }
  m.stopFrames = () => { m.fr.on = false; return { d: m.fr.d, max: m.fr.max } }
  m.watchStatus = () => {
    const el = document.querySelector('[role=status]')
    new MutationObserver(() => m.status.push([performance.now(), el.textContent])).observe(el, { childList: true, subtree: true, characterData: true })
  }
}

async function open(ids, { ctx, deviceMemory } = {}) {
  ctx ??= await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  const errors = [], reqs = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('request', (r) => { if (r.url().includes('/api/')) reqs.push({ t: Date.now(), u: r.url() }) })
  // 模拟内存较小的设备：解码缓存预算 = clamp(deviceMemory × 128 MB, 384, 1024)
  if (deviceMemory) await page.addInitScript((m) => Object.defineProperty(Navigator.prototype, 'deviceMemory', { get: () => m, configurable: true }), deviceMemory)
  await page.addInitScript(collectors)
  await page.goto(`${URL0}/ui/#/compare?ids=${ids.join(',')}&perf=1`)
  // 鼠标初始在 (0, 0)，会让左侧导航栏悬停展开、盖住推理结果栏
  await page.mouse.move(VIEW.width / 2, 20)
  const origin = await page.evaluate(() => performance.timeOrigin)
  return { ctx, page, errors, reqs, origin, ids, epoch: (t) => origin + t, now: () => page.evaluate(() => performance.now()) }
}
const close = async (s) => { await s.ctx.close() }

const HEADS = () => [...document.querySelectorAll('.cmp-col-head .lbl')].map((e) => e.textContent)
/** 视口内的格子都画出了当前图（不是换图后最多保留 100 ms 的上一张）的指定层级（或文件不存在） */
function tiersReady(want) {
  const ok = want === 'lossless' ? ['lossless'] : ['preview', 'lossless']
  return [...document.querySelectorAll('figure.cmp-cell')].every((f) => {
    const r = f.querySelector('.cmp-vp').getBoundingClientRect()
    if (r.bottom <= 0 || r.top >= innerHeight || r.width < 2 || r.height < 2) return true
    const c = f.querySelector('canvas')
    if (c.dataset.pic !== f.dataset.pic) return false
    return ok.includes(c.dataset.tier) || f.querySelector('.cmp-tier')?.textContent === '文件不存在'
  })
}
const waitTiers = async (s, want = 'preview', timeout = 30000) =>
  (await s.page.waitForFunction(([fn, w]) => (new Function(`return (${fn})`)()(w) ? performance.now() : 0), [tiersReady.toString(), want], { polling: 'raf', timeout })).jsonValue()
const waitHeads = async (s, re, n, timeout = 30000) =>
  (await s.page.waitForFunction(([src, n]) => { const h = [...document.querySelectorAll('.cmp-col-head .lbl')].map((e) => e.textContent); return h.length === n && h.every((x) => new RegExp(src).test(x)) ? performance.now() : 0 }, [re.source, n], { polling: 'raf', timeout })).jsonValue()
/** 请求都结束并保持 ms 毫秒 */
async function idle(s, ms = 400, timeout = 60000) {
  const end = Date.now() + timeout
  let since = 0
  for (;;) {
    const busy = await s.page.evaluate(() => window.__cmpPerf ? window.__cmpPerf.total.inflight + window.__cmpPerf.decodes.active : 0)
    if (busy) since = 0
    else if (!since) since = Date.now()
    else if (Date.now() - since >= ms) return
    if (Date.now() > end) throw new Error('等待请求结束超时')
    await s.page.waitForTimeout(50)
  }
}
const longTasks = (s, t0, t1 = Infinity) => s.page.evaluate(([a, b]) => window.__m.lt.filter(([t, d]) => t + d > a && t < b).map(([, d]) => d), [t0, t1])
const marks = (s) => s.page.evaluate(() => performance.getEntriesByType('mark').filter((m) => m.name.startsWith('cmp:')).map((m) => [m.name, m.startTime]))
const perfOf = (s) => s.page.evaluate(() => JSON.parse(JSON.stringify({ renders: __cmpPerf.renders, lanes: __cmpPerf.lanes, total: __cmpPerf.total, decodes: __cmpPerf.decodes, bytes: __cmpPerf.bitmapBytes, pinned: __cmpPerf.pinnedBytes, count: __cmpPerf.bitmapCount })))
const reqsIn = (s, t0, t1 = Infinity, f = () => true) => s.reqs.filter((r) => r.t >= s.epoch(t0) && r.t <= s.epoch(t1) && f(r.u))
const frameStats = (d) => ({ n: d.length, p50: r1(pct(d, 0.5)), p95: r1(pct(d, 0.95)), p99: r1(pct(d, 0.99)), max: r1(Math.max(0, ...d)) })
/** 画布像素（每 97 个字节取一个）；给了 w、h 时只取左上角这一块 */
async function cellPixels(s, k, w, h) {
  return s.page.evaluate(([k, w, h]) => {
    const c = document.querySelectorAll('figure.cmp-cell canvas')[k]
    return { w: c.width, h: c.height, px: Array.from(c.getContext('2d').getImageData(0, 0, w ?? c.width, h ?? c.height).data.filter((_, i) => i % 97 === 0)) }
  }, [k, w, h])
}
const canvasSize = (s, k) => s.page.evaluate((k) => { const c = document.querySelectorAll('figure.cmp-cell canvas')[k]; return [c.width, c.height] }, k)
/** 画布中心附近是否正好是图案 px(x, y) 的原图像素（由 R、G 反解 x、y 再校验 B，插值混合的像素通不过） */
async function exactAt(s, k, seed) {
  return s.page.evaluate(([k, seed]) => {
    const c = document.querySelectorAll('figure.cmp-cell canvas')[k]
    const g = c.getContext('2d')
    return [[0, 0], [3, 0], [0, 3], [-3, -3]].every(([dx, dy]) => {
      const [r, gg, b] = g.getImageData((c.width >> 1) + dx, (c.height >> 1) + dy, 1, 1).data
      const x = (183 * (r - seed)) & 255, y = (205 * gg) & 255
      return b === (((x ^ y) * 3) & 255)
    })
  }, [k, seed])
}
async function ctrlWheel(s, k, steps, dy, gap = 16) {
  const vp = await s.page.locator('figure.cmp-cell .cmp-vp').nth(k).boundingBox()
  await s.page.mouse.move(vp.x + vp.width * 0.5, vp.y + vp.height * 0.5)
  await s.page.keyboard.down('Control')
  for (let i = 0; i < steps; i++) { await s.page.mouse.wheel(0, dy); await s.page.waitForTimeout(gap) }
  await s.page.keyboard.up('Control')
  return vp
}
const zoomText = (s) => s.page.locator('span.mono', { hasText: /^\d+%$/ }).first().textContent()
async function scrollToRow(s, i) {
  await s.page.locator('.cmp-vlist').evaluate((el, i) => { el.scrollTop = Math.max(0, i * 112 - 2 * 112) }, i)
  await s.page.waitForFunction((i) => document.querySelectorAll(`.cmp-thumb[data-i="${i}"] img`).length >= document.querySelectorAll('.cmp-col-head').length, i, { timeout: 30000 })
}
/** 每张图（路径存在的）在 [t, end) 内第一次画出 tier 以上层级的时间差 */
function paintLatency(ms, ids, t, end, tiers) {
  return ids.map((id) => {
    const hit = ms.find(([name, at]) => at >= t && at < end && name.startsWith(`cmp:paint:${id}:`) && tiers.includes(name.split(':')[3]))
    return hit ? hit[1] - t : Infinity
  })
}

// ---------------------------------------------------------------- 场景
const SC = {}

SC.S1 = async () => {
  const out = []
  for (let k = 0; k < RUNS; k++) {
    const s = await open(MAIN)
    const usable = await waitHeads(s, /^1 \/ \d+$/, 6)
    const ready = await waitTiers(s)
    const idxAt = await s.page.evaluate(() => Math.max(...performance.getEntriesByType('resource').filter((e) => /\/images$/.test(e.name)).map((e) => e.responseEnd)))
    await idle(s)
    const lt = await longTasks(s, idxAt)
    out.push({ usable, ready, idxAt, ltMax: Math.max(0, ...lt), lt: lt.length, samples: s.reqs.filter((r) => r.u.includes('/compare/samples')).length, errors: s.errors })
    await close(s)
  }
  results.S1 = out
  console.log('S1', JSON.stringify(out.map((o) => ({ usable: r1(o.usable), ready: r1(o.ready), ltMax: r1(o.ltMax) }))))
  gate('S1', '列表可用（ms，中位数）', med(out.map((o) => o.usable)), 1500)
  gate('S1', '索引到达后最长任务（ms，最大）', Math.max(...out.map((o) => o.ltMax)), 50)
  gate('S1', '/compare/samples 请求数', out.reduce((a, o) => a + o.samples, 0), 0, '==')
}

/** 缩略图栏滚轮滚动 120 × 600 px，再甩到底 */
async function scrollRun() {
  const s = await open(MAIN)
  await waitTiers(s)
  await idle(s)
  await s.page.evaluate(() => window.__cmpPerf.reset())
  const box = await s.page.locator('.cmp-vlist').boundingBox()
  await s.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  const viewRows = Math.floor((box.height - 60) / 112)
  await s.page.evaluate(() => window.__m.startFrames('.cmp-thumb'))
  const t0 = await s.now()
  for (let i = 0; i < 120; i++) { await s.page.mouse.wheel(0, 600); await s.page.waitForTimeout(16) }
  await s.page.waitForTimeout(400)
  const f0 = await s.now()
  for (let i = 0; i < 50; i++) { await s.page.mouse.wheel(0, 50000); await s.page.waitForTimeout(8) }
  await s.page.waitForFunction(() => { const el = document.querySelector('.cmp-vlist'); return el.scrollTop + el.clientHeight >= el.scrollHeight - 2 }, null, { timeout: 10000 })
  const f1 = await s.now()
  await s.page.waitForTimeout(1500)
  const fr = await s.page.evaluate(() => window.__m.stopFrames())
  const t1 = await s.now()
  const p = await perfOf(s)
  const ltAt = await s.page.evaluate(([a, b]) => window.__m.lt.filter(([t, d]) => t + d > a && t < b).map(([t, d]) => [Math.round(t - a), Math.round(d)]), [t0, t1])
  // 甩动期间（到停下后 80 ms 的空闲门限为止）开始的缩略图请求
  const fling = reqsIn(s, f0, f1 + 80, (u) => kindOf(u) === 'thumb').length
  const after = reqsIn(s, f1 + 80, t1, (u) => kindOf(u) === 'thumb').length
  const r = { frames: frameStats(fr.d), lt: ltAt, phases: [f0 - t0, f1 - t0, t1 - t0].map(Math.round), maxThumbs: fr.max, thumbMax: p.lanes.thumb.max, totalMax: p.total.max, fling, afterFling: after, visible: viewRows * 6, renders: p.renders, errors: s.errors }
  console.log('S2', JSON.stringify(r))
  await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S2.png`) })
  await close(s)
  return r
}
SC.S2 = async () => {
  const out = []
  for (let k = 0; k < RUNS; k++) out.push(await scrollRun())
  results.S2 = out
  gate('S2', '长任务数（中位数）', med(out.map((r) => r.lt.length)), 0, '==')
  gate('S2', '帧间隔 p95（ms，中位数）', med(out.map((r) => r.frames.p95)), 20)
  gate('S2', '帧间隔 p99（ms，中位数）', med(out.map((r) => r.frames.p99)), 34)
  gate('S2', '挂载的 .cmp-thumb 最大数', Math.max(...out.map((r) => r.maxThumbs)), 200)
  gate('S2', '缩略图并发最大', Math.max(...out.map((r) => r.thumbMax)), 3)
  gate('S2', '媒体并发最大', Math.max(...out.map((r) => r.totalMax)), 5)
  gate('S2', '甩动期间开始的缩略图请求（最大）', Math.max(...out.map((r) => r.fling)), 2 * out[0].visible)
}

/** 方向键 50 次（间隔 150 ms）：各格新预览画出的延迟、keydown 的 Event Timing */
async function arrowRun(s, missing) {
  await s.page.evaluate(() => { performance.clearMarks(); window.__m.keys.length = 0; window.__m.startFrames() })
  const t0 = await s.now()
  for (let i = 0; i < 50; i++) { await s.page.keyboard.press('ArrowRight'); await s.page.waitForTimeout(150) }
  await waitTiers(s)
  await s.page.waitForTimeout(300)
  const t1 = await s.now()
  const fr = await s.page.evaluate(() => window.__m.stopFrames())
  const keys = await s.page.evaluate(() => window.__m.keys.slice())
  const ms = await marks(s)
  const start = Number((await s.page.evaluate(HEADS))[0].split(' / ')[0]) - 1 - keys.length
  const lat = []
  keys.forEach((t, k) => {
    const end = keys[k + 1] ?? t + 10000, i = start + k + 1
    paintLatency(ms, s.ids, t, end, ['preview', 'lossless']).forEach((d, c) => { if (!missing(s.ids[c], i)) lat.push(d) })
  })
  const ev = await s.page.evaluate(([a, b]) => window.__m.ev.filter(([n, t]) => n === 'keydown' && t >= a && t <= b).map(([, , d]) => d), [t0, t1])
  // 低于 16 ms 的事件不上报，按 0 计
  const evAll = [...ev, ...Array(Math.max(0, keys.length - ev.length)).fill(0)]
  return { lat, ev: evAll, lt: await longTasks(s, t0, t1), frames: frameStats(fr.d), keys: keys.length }
}
const missingIn = (id, i) => lists[id]?.[i]?.[1] === -1

SC.S3 = async () => {
  for (const id of MAIN) await list(id)
  const runs = []
  for (let k = 0; k <= RUNS; k++) {
    // 第一次为冷（Agent 还没生成过这些预览），其后每次用新的浏览器上下文（服务端缓存热、浏览器缓存冷）
    const s = await open(MAIN)
    await waitTiers(s)
    await idle(s)
    const r = await arrowRun(s, missingIn)
    if (k === 0) await warmUp(MAIN, 0, 82, previewSizeOf(s)) // 50 步加 S3b 的 30 步
    runs.push({ cold: k === 0, p50: r1(pct(r.lat, 0.5)), p95: r1(pct(r.lat, 0.95)), miss: r.lat.filter((d) => d === Infinity).length, ev95: pct(r.ev, 0.95), lt: r.lt, frames: r.frames })
    console.log('S3', JSON.stringify(runs.at(-1)))
    if (k === RUNS) {
      // S3b：连按 30 次（间隔 33 ms）
      await waitTiers(s)
      await idle(s)
      await s.page.evaluate(() => { window.__m.keys.length = 0 })
      const before = Number((await s.page.evaluate(HEADS))[0].split(' / ')[0]) - 1
      const t0 = await s.now()
      for (let i = 0; i < 30; i++) { await s.page.keyboard.press('ArrowRight'); await s.page.waitForTimeout(33) }
      const t1 = await s.now()
      await waitTiers(s)
      await idle(s)
      const heads = await s.page.evaluate(HEADS)
      const skipped = reqsIn(s, t0, Infinity, (u) => kindOf(u) === 'preview').filter((r) => { const i = idxOf(r.u); return i > before && i < before + 29 })
      const lt = await longTasks(s, t0, t1 + 500)
      results.S3b = { before, heads, skipped: skipped.length, lt }
      console.log('S3b', JSON.stringify(results.S3b))
      gate('S3b', '长任务数', lt.length, 0, '==')
      gate('S3b', '最终序号正确的列数', heads.filter((h) => h.startsWith(`${before + 31} / `)).length, 6, '==')
      gate('S3b', '被跳过序号的预览请求', skipped.length, 18)
      await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S3.png`) })
    }
    await close(s)
  }
  results.S3 = runs
  const warm = runs.filter((r) => !r.cold)
  gate('S3', '新预览画出 p95（ms，热，中位数）', med(warm.map((r) => r.p95)), 50)
  gate('S3', 'keydown Event Timing p95（ms）', med(warm.map((r) => r.ev95)), 50)
  gate('S3', '长任务数（热）', warm.reduce((a, r) => a + r.lt.length, 0), 0, '==')
}

SC.S4 = async () => {
  for (const id of MAIN) await list(id)
  const out = []
  for (const phase of ['cold', 'warm']) {
    const s = await open(MAIN)
    await waitTiers(s)
    await idle(s)
    await scrollToRow(s, 10000)
    await idle(s)
    await s.page.evaluate(() => { performance.clearMarks(); window.__m.clicks.length = 0 })
    await s.page.locator(`.cmp-thumb[data-id="${MAIN[0]}"][data-i="10000"]`).click({ modifiers: ['Control'] })
    await waitHeads(s, /^10001 \/ \d+$/, 6)
    await waitTiers(s)
    const t = (await s.page.evaluate(() => window.__m.clicks.at(-1)))
    const ms = await marks(s)
    const first = paintLatency(ms, MAIN, t, Infinity, ['thumb', 'preview', 'lossless'])
    const prev = paintLatency(ms, MAIN, t, Infinity, ['preview', 'lossless'])
    const r = { phase, placeholderMax: r1(Math.max(...first)), previewMax: r1(Math.max(...prev)), previewP50: r1(med(prev)) }
    if (phase === 'warm') {
      // Alt+单击：从 _sr 结果集（set2）点，其余 4 组没有同名、由 Worker 按编辑距离找；再从 set0 点（2 组走 Worker）
      for (const [col, i] of [[2, 5000], [0, 7001]]) {
        await scrollToRow(s, i)
        await idle(s)
        await s.page.evaluate(() => { window.__m.status.length = 0; window.__m.watchStatus() })
        const name = baseName(lists[MAIN[col]][i][0])
        const t0 = await s.now()
        await s.page.locator(`.cmp-thumb[data-id="${MAIN[col]}"][data-i="${i}"]`).click({ modifiers: ['Alt'] })
        const tc = await s.page.evaluate(() => window.__m.clicks.at(-1))
        await s.page.waitForFunction((n) => document.querySelector('[role=status]').textContent === `Alt 文件名匹配 · ${n}`, name, { timeout: 10000 })
        const st = await s.page.evaluate((n) => window.__m.status.find(([, x]) => x === `Alt 文件名匹配 · ${n}`)?.[0], name)
        await s.page.waitForTimeout(100)
        const heads = (await s.page.evaluate(HEADS)).map((h) => Number(h.split(' / ')[0]) - 1)
        const want = MAIN.map((id, c) => (c === col ? i : naive(name, lists[id])))
        const lt = await longTasks(s, t0, (await s.now()))
        r[`alt${col}`] = { ms: r1(st - tc), equal: JSON.stringify(heads) === JSON.stringify(want), heads, want, ltMax: r1(Math.max(0, ...lt)) }
      }
    }
    out.push(r)
    console.log('S4', JSON.stringify(r))
    await close(s)
  }
  results.S4 = out
  const w = out[1]
  gate('S4', '占位画出（ms，最大，热）', w.placeholderMax, 33)
  gate('S4', '预览画出（ms，最大，Agent 缓存热）', w.previewMax, 150)
  for (const c of [2, 0]) {
    gate('S4', `Alt（从第 ${c + 1} 组）结果应用（ms）`, w[`alt${c}`].ms, 300)
    gate('S4', `Alt（从第 ${c + 1} 组）与原算法一致`, w[`alt${c}`].equal ? 1 : 0, 1, '==')
    gate('S4', `Alt（从第 ${c + 1} 组）最长任务（ms）`, w[`alt${c}`].ltMax, 50)
  }
}

/** 缩放平移：滚轮、拖动、1600% 像素校验、换图后无损层时间、100% 时不请求无损层 */
async function zoomRun(k) {
  const s = await open(MAIN)
  await waitTiers(s)
  await idle(s)
  const p0 = await perfOf(s)
  await s.page.evaluate(() => window.__m.startFrames())
  const t0 = await s.now()
  // 滚轮 60 次：放大 40 次、缩小 20 次，停在约 260%
  const vp = await ctrlWheel(s, 0, 40, -60)
  await ctrlWheel(s, 0, 20, 60)
  const tw = await s.now()
  const tl0 = await waitTiers(s, 'lossless', 15000)
  // 拖动 60 次
  await s.page.mouse.move(vp.x + vp.width / 2, vp.y + vp.height / 2)
  await s.page.keyboard.down('Control')
  await s.page.mouse.down()
  for (let i = 1; i <= 60; i++) { await s.page.mouse.move(vp.x + vp.width / 2 - i * 4, vp.y + vp.height / 2 - i * 2); await s.page.waitForTimeout(16) }
  await s.page.mouse.up()
  await s.page.keyboard.up('Control')
  const t1 = await s.now()
  const fr = await s.page.evaluate(() => window.__m.stopFrames())
  const p1 = await perfOf(s)
  const lt = await longTasks(s, t0, t1)
  const ltAt = await s.page.evaluate(([a, b]) => window.__m.lt.filter(([t, d]) => t + d > a && t < b).map(([t, d]) => [Math.round(t - a), Math.round(d)]), [t0, t1])
  const z = await zoomText(s)
  await waitTiers(s, 'lossless', 15000)
  if (!k) await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S5-zoom.png`) })
  // 放大到 1600%：中心像素是原图像素（图案图走整张原图）
  await ctrlWheel(s, 0, 10, -400)
  await s.page.waitForFunction(() => [...document.querySelectorAll('span.mono')].some((e) => e.textContent === '1600%'))
  await waitTiers(s, 'lossless', 15000)
  const exact = []
  for (let k = 0; k < 6; k++) exact.push(await exactAt(s, k, seedOf(MAIN[k])))
  if (!k) await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S5-1600.png`) })
  // 回到约 260%，换到第 2 张（4K 照片，瓦片），再到第 8 张（8K，冷解码）
  await ctrlWheel(s, 0, 12, 100)
  await waitTiers(s, 'lossless', 15000)
  await idle(s)
  await s.page.keyboard.press('ArrowRight')
  const ta = await s.page.evaluate(() => window.__m.keys.at(-1))
  const t4k = (await waitTiers(s, 'lossless', 20000)) - ta
  await idle(s)
  for (let i = 0; i < 6; i++) { await s.page.keyboard.press('ArrowRight'); await s.page.waitForTimeout(33) }
  const tb = await s.page.evaluate(() => window.__m.keys.at(-1))
  const t8k = (await waitTiers(s, 'lossless', 20000)) - tb
  const at8 = (await s.page.evaluate(HEADS))[0]
  if (!k) await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S5-8k.png`) })
  // 适应窗口后换图：没有瓦片和原图请求
  await s.page.getByRole('button', { name: '适应窗口' }).click()
  await waitTiers(s)
  await idle(s)
  const tf = await s.now()
  for (const k of ['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowLeft']) { await s.page.keyboard.press(k); await s.page.waitForTimeout(300) }
  await waitTiers(s)
  await idle(s)
  const lossless1 = reqsIn(s, tf, Infinity, (u) => ['tile', 'full'].includes(kindOf(u))).length
  const fs = frameStats(fr.d)
  const r = { frames: fs, lt: ltAt, phases: [tw - t0, tl0 - t0, t1 - t0].map(Math.round), zoom: z, losslessAfterWheel: r1(tl0 - tw), exact, t4k: r1(t4k), t8k: r1(t8k), at8, lossless1, thumbPanel: (p1.renders.thumbPanel ?? 0) - (p0.renders.thumbPanel ?? 0), cell: (p1.renders.cell ?? 0) - (p0.renders.cell ?? 0), errors: s.errors }
  console.log('S5', JSON.stringify(r))
  await close(s)
  return r
}
SC.S5 = async () => {
  // 第一次 Agent 缓存冷（第 8 张 8K 冷解码只在这次测得），其后服务端缓存热
  const out = []
  for (let k = 0; k < RUNS; k++) out.push(await zoomRun(k))
  results.S5 = out
  const [c] = out
  gate('S5', '帧间隔 p95（ms，中位数）', med(out.map((r) => r.frames.p95)), 20)
  gate('S5', 'ThumbPanel 提交次数', Math.max(...out.map((r) => r.thumbPanel)), 0, '==')
  gate('S5', '长任务数（中位数）', med(out.map((r) => r.lt.length)), 0, '==')
  gate('S5', '缩放后全部无损（ms，图案 4K，中位数）', med(out.map((r) => r.losslessAfterWheel)), 1500)
  gate('S5', '换图后全部无损（ms，4K 瓦片，中位数）', med(out.map((r) => r.t4k)), 1500)
  gate('S5', '换图后全部无损（ms，8K 冷解码，第一次）', c.t8k, 2500)
  gate('S5', '1600% 中心像素精确的格子数（最少）', Math.min(...out.map((r) => r.exact.filter(Boolean).length)), 6, '==')
  gate('S5', '100% 时的瓦片 / 原图请求', Math.max(...out.map((r) => r.lossless1)), 0, '==')
}

SC.S6 = async () => {
  const s = await open(MAIN)
  await waitTiers(s)
  await idle(s, 800)
  const out = []
  for (const zoomed of [false, true]) {
    if (zoomed) {
      await ctrlWheel(s, 0, 10, -100)
      await waitTiers(s, 'lossless', 20000)
      await idle(s, 800)
    }
    const n0 = s.reqs.length
    const t0 = await s.now()
    await s.page.evaluate(() => { window.__m.holds.length = 0 })
    for (let k = 0; k < (zoomed ? 6 : 20); k++) {
      const c = k % 6, b = k % 5
      const other = MAIN.filter((id) => id !== MAIN[c])[b]
      await s.page.locator('figure.cmp-cell').nth(c).locator('.cmp-hold').nth(b).hover()
      await s.page.mouse.down()
      await s.page.waitForTimeout(50)
      // 画布按各自的设备像素尺寸分配，落在小数像素位置的格子可能差 1 个设备像素：比较两者共有的左上角区域
      const [aw, ah] = await canvasSize(s, c), [ow, oh] = await canvasSize(s, MAIN.indexOf(other))
      const w = Math.min(aw, ow), h = Math.min(ah, oh)
      const a = await cellPixels(s, c, w, h), o = await cellPixels(s, MAIN.indexOf(other), w, h)
      const label = await s.page.locator('figure.cmp-cell canvas').nth(c).getAttribute('aria-label')
      out.push({ zoomed, same: JSON.stringify(a.px) === JSON.stringify(o.px), sizeDiff: aw !== ow || ah !== oh, label })
      await s.page.mouse.up()
      await s.page.waitForTimeout(50)
    }
    const holds = await s.page.evaluate(() => window.__m.holds.map(([a, b]) => b - a))
    const lt = await longTasks(s, t0, await s.now())
    results[zoomed ? 'S6zoom' : 'S6'] = { paintMax: r1(Math.max(...holds)), paintP50: r1(med(holds)), fetches: s.reqs.length - n0, sameCount: out.filter((o) => o.zoomed === zoomed && o.same).length, sizeDiff: out.filter((o) => o.zoomed === zoomed && o.sizeDiff).length, lt }
    console.log(zoomed ? 'S6 (放大)' : 'S6', JSON.stringify(results[zoomed ? 'S6zoom' : 'S6']))
    const sc = zoomed ? 'S6 放大' : 'S6'
    const r = results[zoomed ? 'S6zoom' : 'S6']
    gate(sc, '按下到画出（ms，最大）', r.paintMax, 33)
    gate(sc, '请求数', r.fetches, 0, '==')
    gate(sc, '与对方像素一致的次数', r.sameCount, zoomed ? 6 : 20, '==')
    gate(sc, '长任务数', lt.length, 0, '==')
  }
  await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S6.png`) })
  await close(s)
}

/** 看着前 5 组时勾选第 6 组 */
async function addSixth(label) {
  const s = await open(MAIN.slice(0, 5))
  await waitTiers(s)
  await idle(s)
  const name = sets.find((r) => r.id === MAIN[5]).name
  const t0 = await s.now()
  await s.page.locator('label.cmp-set', { hasText: name }).locator('input').check()
  const tc = await s.page.evaluate(() => window.__m.clicks.at(-1))
  const head = await waitHeads(s, /^1 \/ \d+$/, 6)
  const thumb = (await s.page.waitForFunction((id) => document.querySelector(`.cmp-thumb[data-id="${id}"] img`) ? performance.now() : 0, MAIN[5], { polling: 'raf', timeout: 30000 })).jsonValue()
  const tr = await waitTiers(s)
  const lt = await longTasks(s, t0, await s.now())
  const r = { label, head: r1(head - tc), thumb: r1((await thumb) - tc), preview: r1(tr - tc), lt, samples: s.reqs.filter((x) => x.u.includes('/compare/samples')).length }
  await close(s)
  return r
}
SC.S7c = async () => {
  results.S7cold = await addSixth('cold')
  console.log('S7 (冷)', JSON.stringify(results.S7cold))
  gate('S7 冷', '列标题出现计数（ms）', results.S7cold.head, 1500)
}
SC.S7 = async () => {
  const out = []
  for (let k = 0; k < RUNS; k++) out.push(await addSixth('warm'))
  results.S7 = out
  console.log('S7', JSON.stringify(out))
  gate('S7', '列标题出现计数（ms，热，中位数）', med(out.map((o) => o.head)), 300)
  gate('S7', '第一张缩略图（ms，热，中位数）', med(out.map((o) => o.thumb)), 600)
  gate('S7', '长任务数', out.reduce((a, o) => a + o.lt.length, 0), 0, '==')
  gate('S7', '/compare/samples 请求数', out.reduce((a, o) => a + o.samples, 0), 0, '==')
}

SC.S8 = async () => {
  if (K8.some((id) => !id)) { console.log('S8 跳过：没有 k8-0..k8-5 结果集'); return }
  for (const id of K8) await list(id)
  // 按最小预算（384 MB）跑：每步约 26 MB 新位图，第 15 步前后填满，之后靠淘汰（close()）维持；
  // 预算 1024 MB（无头 Chromium 报 deviceMemory 8）时 30 步填不满，RSS 增长量的是缓存填充而不是泄漏，也测不到淘汰
  const s = await open(K8, { deviceMemory: 2 })
  await waitTiers(s, 'preview', 60000)
  await idle(s)
  // 8K 图案（第 1 张）在 1600% 下走第 0 层瓦片：折叠两侧栏让格子够宽（一个原图像素不小于一个设备像素），中心像素精确
  const fold = async (on) => {
    for (const name of on ? ['折叠缩略图栏', '折叠推理结果栏'] : ['展开缩略图栏', '展开推理结果栏']) await s.page.getByRole('button', { name }).click()
    await s.page.waitForTimeout(600)
  }
  await fold(true)
  await ctrlWheel(s, 0, 10, -400)
  await waitTiers(s, 'lossless', 30000)
  const k16 = await s.page.evaluate(() => { const c = document.querySelector('figure.cmp-cell canvas'); return 16 * Math.min(c.width / 7680, c.height / 4320) })
  const exact = []
  for (let k = 0; k < 6; k++) exact.push(await exactAt(s, k, seedOf(K8[k])))
  await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S8-1600.png`) })
  await s.page.getByRole('button', { name: '适应窗口' }).click()
  await fold(false)
  await ctrlWheel(s, 0, 5, -93) // ≈ 200%
  const z = await zoomText(s)
  await waitTiers(s, 'lossless', 30000)
  const steps = []
  let base = null
  for (let k = 1; k <= 30; k++) {
    await s.page.keyboard.press('ArrowRight')
    const tk = await s.page.evaluate(() => window.__m.keys.at(-1))
    let tl = null
    try { tl = (await waitTiers(s, 'lossless', 8000)) - tk } catch { tl = Infinity }
    const p = await perfOf(s)
    const m = k === 5 || k === 30 ? await rss() : null
    if (k === 5) base = m
    steps.push({ k, lossless: r1(tl), bytes: Math.round(p.bytes / 2 ** 20), pinned: Math.round(p.pinned / 2 ** 20), rss: m && Math.round(m.renderer), gpu: m && Math.round(m.gpu) })
  }
  const budget = await s.page.evaluate(() => Math.min(1024, Math.max(384, (navigator.deviceMemory ?? 4) * 128)))
  await s.page.screenshot({ path: join(OUT, `perf-${TAG}-S8.png`) })
  // 取消全部结果集
  for (const id of K8) await s.page.locator('label.cmp-set', { hasText: sets.find((r) => r.id === id).name }).locator('input').uncheck()
  await s.page.getByText('在左侧勾选至少一个推理结果').waitFor()
  await s.page.waitForTimeout(500)
  const end = await perfOf(s)
  const last = steps.at(-1)
  // 解码缓存在预算内正常增长（第 5 步到第 30 步）；扣掉它之后的增长才可能是泄漏
  const step5 = steps[4]
  const otherGrowth = last.rss - base.renderer - (last.bytes - step5.bytes)
  results.S8 = { zoom: z, k16: r1(k16), exact, budget, steps, endBytes: end.bytes, endPinned: end.pinned, rssGrowth: last.rss - base.renderer, otherGrowth, errors: s.errors }
  console.log('S8', JSON.stringify(results.S8))
  gate('S8', '1600% 中心像素精确的格子数（瓦片）', exact.filter(Boolean).length, 6, '==')
  gate('S8', '位图字节 − pin 住的（MB，最大）', Math.max(...steps.map((x) => x.bytes - x.pinned)), budget)
  gate('S8', '渲染进程 RSS 增长（第 5→30 步，MB）', last.rss - base.renderer, 300)
  gate('S8', '渲染进程 RSS 增长扣除解码缓存增长（MB）', otherGrowth, 300)
  gate('S8', '取消全部后的位图字节', end.bytes, 0, '==')
  await close(s)
}

try {
  for (const k of ONLY) {
    if (!SC[k]) throw new Error(`没有场景 ${k}`)
    console.log(`== ${k}`)
    await SC[k]()
  }
} finally {
  await browser.close()
  const file = join(OUT, `perf-${TAG}.json`)
  writeFileSync(file, JSON.stringify({ tag: TAG, url: URL0, results, gates }, null, 2))
  const failed = gates.filter((g) => !g.pass)
  console.log(`\n${gates.length - failed.length}/${gates.length} 通过；结果写到 ${file}`)
  for (const g of failed) console.log(`  FAIL ${g.sc} ${g.name}: ${g.value} (${g.limit})`)
  if (failed.length) process.exitCode = 1
}
