// 对比页示例数据模式的冒烟测试：逐项操作 PR #23 的全部交互，检查无页面错误并截图。
// 不依赖项目里的 npm 包，使用全局安装的 playwright 与 /opt/pw-browsers 里的浏览器（不要 playwright install）。
// 脚本把所有 /api 请求拦下回 404 {"detail":"Not Found"}，前端随即切到示例数据；只需先启动前端：
//   cd web && npx vite --port 5180
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node e2e/compare-mock.mjs [http://localhost:5180/ui/]
// NO_ROUTE=1 时不拦截，/api 经 vite 代理到 127.0.0.1:8000，那里需要一个对所有请求回 404 {"detail":"Not Found"} 的服务。
// 截图写到 $OUT_DIR（默认系统临时目录）下的 fe-*.png
import assert from 'node:assert/strict'
import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

process.env.PLAYWRIGHT_BROWSERS_PATH ??= '/opt/pw-browsers'
const require = createRequire(import.meta.url)
const { chromium } = require(join(execSync('npm root -g').toString().trim(), 'playwright'))
const BASE = process.argv[2] ?? 'http://localhost:5180/ui/'
const OUT = process.env.OUT_DIR ?? tmpdir()

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
const naive = (q, names) => names.reduce((best, f, k) => (editDistance(q, f) < editDistance(q, names[best]) ? k : best), 0)
const mockNames = (n) => Array.from({ length: n }, (_, i) => `${String(i).padStart(4, '0')}.png`)

const browser = await chromium.launch({ args: ['--force-color-profile=srgb'] })
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
if (process.env.NO_ROUTE !== '1') await page.route((u) => u.pathname.startsWith('/api/'), (r) => r.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }))
// 页面里按应用实际加载的地址导入模块（开发服务器改过的文件带 ?t=，直接写路径会得到另一份模块实例）
await page.addInitScript(() => {
  window.__mod = (p) => import(performance.getEntriesByType('resource').map((e) => e.name).find((n) => new URL(n).pathname === `/ui/src/${p}`) ?? `/ui/src/${p}`)
})
const errors = []
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
// 接口 404 切到示例数据时浏览器会打印资源加载失败，属预期
page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource: .* 404/.test(m.text())) errors.push(`console: ${m.text()}`) })

const step = (s) => console.log(`· ${s}`)
const shot = (name) => page.screenshot({ path: join(OUT, `fe-${name}.png`) })
const cells = page.locator('figure.cmp-cell')
// 画布上还是换图前的上一张（最多保留 100 ms）时记为 'stale'，不算画出了当前图
const tiers = () => page.$$eval('figure.cmp-cell', (fs) => fs.map((f) => { const c = f.querySelector('canvas'); return c.dataset.pic === f.dataset.pic ? c.dataset.tier ?? '' : 'stale' }))
const counts = () => page.$$eval('figure.cmp-cell figcaption .mono.lbl', (es) => es.map((e) => e.textContent))
const status = () => page.locator('[role=status]').textContent()
const perf = () => page.evaluate(() => JSON.parse(JSON.stringify({ renders: window.__cmpPerf.renders, lanes: window.__cmpPerf.lanes, total: window.__cmpPerf.total, bytes: window.__cmpPerf.bitmapBytes })))
async function until(fn, what, ms = 8000) {
  const end = Date.now() + ms
  for (;;) {
    const v = await fn()
    if (v) return v
    if (Date.now() > end) throw new Error(`超时：${what}`)
    await page.waitForTimeout(50)
  }
}
const allTier = (t) => until(async () => (await tiers()).every((x) => x === t), `全部格子 data-tier=${t}`)
/** 画布中心像素是否正好是示例数据的某个原图像素（插值混合的像素通不过：由 R、G 反解出 x、y，再校验 B） */
async function exactAt(k) {
  return page.evaluate(async (k) => {
    const { mockSeed } = await window.__mod('mock/images.ts')
    const fig = document.querySelectorAll('figure.cmp-cell')[k]
    const c = fig.querySelector('canvas')
    const id = Number(fig.dataset.id)
    const path = `images/${fig.querySelector('.row .mono.ellipsis.grow').textContent}`
    const seed = mockSeed(id, path)
    const out = []
    for (const [dx, dy] of [[0, 0], [3, 0], [0, 3], [-3, -3]]) {
      const [r, g, b] = c.getContext('2d').getImageData((c.width >> 1) + dx, (c.height >> 1) + dy, 1, 1).data
      const x = (183 * (r - seed)) & 255, y = (205 * g) & 255
      out.push(b === (((x ^ y) * 3) & 255))
    }
    return out.every(Boolean)
  }, k)
}
const pixels = (k) => page.evaluate((k) => {
  const c = document.querySelectorAll('figure.cmp-cell canvas')[k]
  return Array.from(c.getContext('2d').getImageData(0, 0, c.width, c.height).data.filter((_, i) => i % 97 === 0))
}, k)
/** 两个格子画布共有的左上角区域像素是否相同（画布按各自的设备像素尺寸分配，可能差 1 个设备像素） */
const samePixels = (a, b) => page.evaluate(([a, b]) => {
  const cs = document.querySelectorAll('figure.cmp-cell canvas'), w = Math.min(cs[a].width, cs[b].width), h = Math.min(cs[a].height, cs[b].height)
  const [x, y] = [cs[a], cs[b]].map((c) => c.getContext('2d').getImageData(0, 0, w, h).data)
  return x.every((v, i) => v === y[i])
}, [a, b])

try {
  step('打开 ids=1,2,3（示例数据）')
  await page.goto(`${BASE}#/compare?ids=1,2,3&perf=1`)
  await page.getByText('示例数据 · 对比接口未上线').waitFor()
  await until(async () => (await cells.count()) === 3, '3 个格子')
  await allTier('preview')
  assert.deepEqual(await counts(), ['1 / 100', '1 / 100', '1 / 100'])
  await until(async () => (await page.locator('.cmp-thumb img').count()) >= 9, '缩略图加载')
  assert.ok((await page.locator('.cmp-thumb').count()) <= 200)
  await shot('01-initial')

  step('单击：只切换该结果集')
  await page.locator('.cmp-thumb[data-id="2"][data-i="5"]').click()
  assert.deepEqual(await counts(), ['1 / 100', '6 / 100', '1 / 100'])
  assert.match(await status(), /^仅切换 sr-x4-v2-perceptual · 0005\.png$/)
  // 对齐基准换成结果集 2 后，另外两格编号不一致
  await until(async () => (await page.getByText('编号不一致').count()) === 2, '编号不一致角标')
  await allTier('preview')
  assert.equal(await page.locator('figure.cmp-cell').nth(1).locator('.row .mono.ellipsis.grow').textContent(), '0005.png')
  await shot('02-click')

  step('Ctrl+单击：按序号同步全部')
  await page.locator('.cmp-thumb[data-id="1"][data-i="8"]').click({ modifiers: ['Control'] })
  assert.deepEqual(await counts(), ['9 / 100', '9 / 100', '9 / 100'])
  assert.match(await status(), /^Ctrl 按序号同步 · 第 9 张$/)
  assert.equal(await page.getByText('编号不一致').count(), 0)

  step('方向键与上一张 / 下一张按钮')
  for (let k = 0; k < 3; k++) await page.keyboard.press('ArrowRight')
  assert.deepEqual(await counts(), ['12 / 100', '12 / 100', '12 / 100'])
  assert.match(await status(), /^下一张 · 全部结果$/)
  await page.keyboard.press('ArrowUp')
  await page.getByRole('button', { name: '‹ 上一张' }).click()
  assert.deepEqual(await counts(), ['10 / 100', '10 / 100', '10 / 100'])
  await page.getByRole('button', { name: '下一张 ›' }).click()
  assert.deepEqual(await counts(), ['11 / 100', '11 / 100', '11 / 100'])
  await allTier('preview')
  // 序号逢 10 的示例图是 8K，格子较小时 1600% 仍不到 1 设备像素/原图像素；换到 4K 的第 12 张
  await page.keyboard.press('ArrowRight')
  await allTier('preview')

  step('按住切换：画面换成对方的当前图，不发请求')
  const before = await perf()
  const holdBtn = cells.nth(0).getByRole('button', { name: '按住查看 sr-x4-v2-perceptual 的当前图' })
  await holdBtn.hover()
  await page.mouse.down()
  await page.getByText('正在显示 sr-x4-v2-perceptual').waitFor()
  assert.equal(await cells.nth(0).locator('canvas').getAttribute('aria-label'), 'sr-x4-v2-perceptual 0011.png')
  assert.ok(await samePixels(0, 1))
  await shot('03-hold')
  await page.mouse.up()
  await until(async () => (await page.locator('.cmp-holding:not([hidden])').count()) === 0, '松开后恢复')
  assert.equal(await cells.nth(0).locator('canvas').getAttribute('aria-label'), 'sr-x4-baseline 0011.png')
  const afterHold = await perf()
  assert.equal(afterHold.lanes.main.started, before.lanes.main.started)

  step('Ctrl+滚轮缩放：无损瓦片，缩放期间没有 React 提交')
  const vp = await cells.nth(0).locator('.cmp-vp').boundingBox()
  await page.mouse.move(vp.x + vp.width * 0.4, vp.y + vp.height * 0.45)
  await page.keyboard.down('Control')
  for (let k = 0; k < 4; k++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(16) }
  await page.keyboard.up('Control')
  assert.notEqual(await page.locator('.mono', { hasText: /^\d+%$/ }).first().textContent(), '100%')
  await allTier('lossless')
  const z1 = await perf()
  assert.equal(z1.renders.thumbPanel, afterHold.renders.thumbPanel)
  assert.equal(z1.renders.cell, afterHold.renders.cell)
  await shot('04-zoom')

  step('放大到 1600%：画布中心是原图像素（无插值）')
  await page.keyboard.down('Control')
  for (let k = 0; k < 6; k++) { await page.mouse.wheel(0, -400); await page.waitForTimeout(16) }
  await page.keyboard.up('Control')
  await until(async () => (await page.getByText('1600%').count()) === 1, '1600%')
  await allTier('lossless')
  assert.equal(await cells.nth(0).locator('canvas').getAttribute('data-level'), '0')
  for (const k of [0, 1, 2]) assert.ok(await exactAt(k), `格子 ${k} 中心像素精确`)
  assert.equal(await cells.nth(0).locator('.cmp-tier').first().textContent(), '原始像素')
  await shot('05-zoom16')

  step('左键拖动平移（不按修饰键），三格同步')
  const p0 = await pixels(0)
  await page.mouse.move(vp.x + vp.width / 2, vp.y + vp.height / 2)
  await page.mouse.down()
  assert.equal(await page.locator('.cmp-grid').getAttribute('data-drag'), '1')
  for (let k = 1; k <= 8; k++) await page.mouse.move(vp.x + vp.width / 2 + k * 15, vp.y + vp.height / 2 + k * 8)
  await page.mouse.up()
  assert.equal(await page.locator('.cmp-grid').getAttribute('data-drag'), null)
  await allTier('lossless')
  assert.notDeepEqual(await pixels(0), p0)
  for (const k of [0, 1, 2]) assert.ok(await exactAt(k), `平移后格子 ${k} 中心像素精确`)
  const z2 = await perf()
  assert.equal(z2.renders.thumbPanel, afterHold.renders.thumbPanel)

  const zoomIn = async () => {
    await page.mouse.move(vp.x + vp.width / 2, vp.y + vp.height / 2)
    await page.keyboard.down('Control')
    for (let k = 0; k < 4; k++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(16) }
    await page.keyboard.up('Control')
    await until(async () => (await page.getByText('100%', { exact: true }).count()) === 0, '已放大')
  }
  const fitted = async (what) => {
    await until(async () => (await page.getByText('100%', { exact: true }).count()) === 1, what)
    await allTier('preview')
  }

  step('放大状态下换图：自动回到 100%，再按住切换')
  await page.keyboard.press('ArrowRight')
  await fitted('方向键换图后回到 100%')
  await cells.nth(1).getByRole('button', { name: '按住查看 sr-x4-baseline 的当前图' }).hover()
  await page.mouse.down()
  assert.ok(await samePixels(1, 0))
  await page.mouse.up()
  await zoomIn()
  await page.locator('.cmp-thumb[data-id="1"][data-i="3"]').click()
  await fitted('单击缩略图换图后回到 100%')

  step('放大状态下连按方向键：位置逐张前进，停下后为 100%')
  await zoomIn()
  const h0 = await counts()
  for (let k = 0; k < 6; k++) { await page.keyboard.press('ArrowRight'); await page.waitForTimeout(33) }
  await fitted('连按后 100%')
  assert.deepEqual(await counts(), h0.map((c) => c.replace(/^\d+/, (x) => String(Number(x) + 6))))

  step('适应窗口：回到 100%，不再请求瓦片')
  await zoomIn()
  await page.getByRole('button', { name: '适应窗口' }).click()
  await fitted('100%')
  const fitStarted = (await perf()).lanes.main.started
  await page.keyboard.press('ArrowRight')
  await allTier('preview')
  await page.waitForTimeout(300)
  assert.ok((await perf()).lanes.main.started - fitStarted <= 6, '100% 时换图只请求缩略图与预览')

  step('折叠缩略图栏与推理结果栏（动画结束后画布按新尺寸重新分配）')
  const w0 = await cells.nth(0).locator('canvas').evaluate((c) => c.width)
  await page.getByRole('button', { name: '折叠缩略图栏' }).click()
  await page.getByRole('button', { name: '折叠推理结果栏' }).click()
  await page.waitForTimeout(600)
  const [cwid, vw] = await cells.nth(0).locator('canvas').evaluate((c) => [c.width, c.parentElement.clientWidth])
  assert.ok(cwid > w0 && Math.abs(cwid - vw) <= 1, `画布 ${cwid} 应等于视口 ${vw}`)
  // 画布的设备像素尺寸与浏览器给出的 device-pixel-content-box 一致（格子在小数像素位置时也不被再次插值）
  const dpcb = await page.$$eval('figure.cmp-cell .cmp-vp', (vps) => Promise.all(vps.map((vp) => new Promise((ok) => {
    const ro = new ResizeObserver((es) => { const d = es[0].devicePixelContentBoxSize[0]; ro.disconnect(); ok([d.inlineSize, d.blockSize, vp.querySelector('canvas').width, vp.querySelector('canvas').height]) })
    ro.observe(vp, { box: 'device-pixel-content-box' })
  }))))
  for (const [dw, dh, bw, bh] of dpcb) assert.ok(dw === bw && dh === bh, `画布 ${bw}×${bh} 应等于设备像素 ${dw}×${dh}`)
  await allTier('preview')
  await shot('06-folded')
  await page.getByRole('button', { name: '展开缩略图栏' }).click()
  await page.getByRole('button', { name: '展开推理结果栏' }).click()
  await page.waitForTimeout(600)
  assert.equal(await cells.nth(0).locator('canvas').evaluate((c) => c.width), w0)

  step('添加结果集：新列与新格子，不影响已有位置')
  const prevCounts = await counts()
  await page.locator('label.cmp-set', { hasText: 'deblur-v1' }).locator('input').check()
  await until(async () => (await cells.count()) === 4, '4 个格子')
  await until(async () => (await counts())[3] === '1 / 1111', '新结果集列表')
  assert.deepEqual((await counts()).slice(0, 3), prevCounts)
  await until(async () => (await tiers())[3] === 'preview', '新格子预览')
  await shot('07-added')

  step('滚动缩略图栏：虚拟行、滚动停下后加载缩略图')
  const list = page.locator('.cmp-vlist')
  await list.evaluate((el) => { el.scrollTop = 60000 })
  await page.waitForTimeout(50)
  await until(async () => (await page.locator('.cmp-thumb[data-id="4"]').first().getAttribute('data-i')) > 400, '滚动后渲染的行')
  assert.ok((await page.locator('.cmp-thumb').count()) <= 200)
  await until(async () => (await page.locator('.cmp-thumb[data-id="4"] img').count()) > 3, '停下后加载缩略图')
  await list.hover()
  for (let k = 0; k < 20; k++) await page.mouse.wheel(0, 600)
  await page.waitForTimeout(400)
  const p3 = await perf()
  assert.ok(p3.lanes.thumb.max <= 3 && p3.total.max <= 5, `并发上限 ${JSON.stringify(p3.lanes)}`)
  await shot('08-scrolled')

  step('当前图不在视口时缩略图栏跟随')
  await page.locator('.cmp-col-head').nth(3).click()
  await page.keyboard.press('ArrowRight')
  await until(async () => list.evaluate((el) => {
    const on = el.querySelector('.cmp-thumb[data-id="4"][aria-current="true"]')
    if (!on) return false
    const a = on.getBoundingClientRect(), b = el.getBoundingClientRect()
    return a.top >= b.top && a.bottom <= b.bottom
  }), '跟随到当前行')

  step('Alt+单击：完全同名立即对齐，其余由 Worker 按编辑距离匹配')
  await page.goto('about:blank')
  await page.goto(`${BASE}#/compare?ids=4,5&perf=1`)
  await until(async () => (await counts()).join() === '1 / 1111,1 / 20', '两个结果集')
  await list.evaluate((el) => { el.scrollTop = 10 * 112 })
  await page.locator('.cmp-thumb[data-id="5"][data-i="15"]').click({ modifiers: ['Alt'] })
  assert.deepEqual(await counts(), ['16 / 1111', '16 / 20'])
  await list.evaluate((el) => { el.scrollTop = 50 * 112 })
  await page.locator('.cmp-thumb[data-id="4"][data-i="57"]').click({ modifiers: ['Alt'] })
  const want = naive('0057.png', mockNames(20))
  await until(async () => (await status()) === 'Alt 文件名匹配 · 0057.png', 'Worker 匹配完成')
  assert.deepEqual(await counts(), ['58 / 1111', `${want + 1} / 20`])
  await shot('09-alt')

  step('悬停预取与切到指标视图再回来')
  const hp = (await perf()).lanes.prefetch.started
  await page.locator('.cmp-thumb[data-id="4"][data-i="60"]').hover()
  await until(async () => (await perf()).lanes.prefetch.started > hp, '悬停预取')
  await page.getByRole('button', { name: '指标', exact: true }).click()
  await page.getByText('样本对比').waitFor()
  await page.getByRole('button', { name: '图片对比' }).click()
  await until(async () => (await counts()).join() === '1 / 1111,1 / 20', '回到图片视图')
  await allTier('preview')
  await shot('10-back')

  step('窗口变化换了预览档位后，预取跟着换档，下一张直接命中')
  await page.goto(`${BASE}#/compare?ids=1&perf=1`)
  await allTier('preview')
  const pvOf = () => page.evaluate(async () => {
    const { previewSize } = await window.__mod('pages/compare/geometry.ts')
    const c = document.querySelector('figure.cmp-cell canvas'), r = c.getBoundingClientRect()
    return previewSize(r.width, r.height, devicePixelRatio)
  })
  const pinnedNext = (size) => page.evaluate(async (size) => {
    const { bitmaps } = await window.__mod('pages/compare/bitmapCache.ts')
    const { peekIndex } = await window.__mod('pages/compare/imageIndex.ts')
    const { resultsApi } = await window.__mod('api/results.ts')
    const ix = peekIndex(1), i = Number(document.querySelector('figure.cmp-cell figcaption .mono.lbl').textContent.split('/')[0])
    return bitmaps.isPinned(resultsApi.imageUrl(1, ix.paths[i], ix.vs[i], 'preview', { size }))
  }, size)
  const pv0 = await pvOf()
  await until(() => pinnedNext(pv0), `预取 ${pv0} 档`)
  // 窗口变窄：单个格子从 2048 档降到 1024 档
  await page.setViewportSize({ width: 1100, height: 1000 })
  await page.waitForTimeout(400)
  const pv1 = await pvOf()
  assert.notEqual(pv1, pv0, '窗口变窄后格子应换到更小的预览档位')
  await until(() => pinnedNext(pv1), `预取换到 ${pv1} 档`)
  await page.keyboard.press('ArrowRight')
  await page.waitForTimeout(40)
  assert.deepEqual(await tiers(), ['preview'])
  await shot('11-narrow')
  await page.setViewportSize({ width: 1600, height: 1000 })

  step('取消全部结果集')
  await page.locator('label.cmp-set', { hasText: 'sr-x4-baseline' }).locator('input').uncheck()
  await page.getByText('在左侧勾选至少一个推理结果').waitFor()
  await until(async () => (await perf()).bytes === 0, '解码缓存清空')

  assert.deepEqual(errors, [])
  console.log(`通过；截图在 ${OUT}/fe-*.png`)
} catch (e) {
  await shot('failure').catch(() => {})
  console.error(errors.join('\n'))
  throw e
} finally {
  await browser.close()
}
