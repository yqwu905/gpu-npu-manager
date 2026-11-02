// Run against Vite with PLAYWRIGHT_MODULE and CHROMIUM_PATH overrides when needed.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { execSync } from 'node:child_process'
import { join } from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || join(execSync('npm root -g').toString().trim(), 'playwright'))
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH })
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, permissions: ['clipboard-read', 'clipboard-write'] })
const page = await context.newPage()
const errors = [], writes = []
page.on('pageerror', (e) => errors.push(e.message))
await page.route((u) => u.pathname.startsWith('/api/'), (route) => {
  if (route.request().method() !== 'GET') writes.push(route.request().url())
  return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' })
})
// Expose the real store without changing its behavior, for viewport-range assertions.
await page.route('**/viewStore.ts*', async (route) => {
  const response = await route.fetch()
  await route.fulfill({ response, body: (await response.text()).replace('return store;', 'window.__testStore = store; return store;') })
})
const cells = page.locator('.cmp-cell')
const vp = cells.first().locator('.cmp-vp')
const status = page.locator('[role=status]')
const wait = async (fn) => { for (let i = 0; i < 200; i++) { if (await fn()) return; await page.waitForTimeout(50) } throw new Error('Timed out') }
const range = () => vp.evaluate((el) => {
  const r = el.getBoundingClientRect(), v = window.__testStore.forSize(r.width, r.height)
  return { z: v.z, x: v.x / r.width, y: v.y / r.height, aspect: r.width / r.height }
})
try {
  await page.goto((process.argv[2] || 'http://127.0.0.1:5180/ui/') + '#/compare?ids=1,2,3&perf=1')
  await wait(async () => await cells.count() === 3 && await cells.locator('canvas[data-tier="preview"]').count() === 3)
  assert.ok(await page.evaluate(() => !!window.__testStore))
  await page.mouse.move(700, 50)
  const buttons = cells.first().locator('.cmp-hold-btns')
  await wait(async () => await buttons.evaluate((el) => getComputedStyle(el).opacity) === '0')
  await vp.hover()
  await wait(async () => await buttons.evaluate((el) => getComputedStyle(el).opacity) === '1')
  const hold = cells.first().locator('.cmp-hold').first()
  await hold.hover(); await page.mouse.down()
  await wait(async () => await cells.first().locator('.cmp-holding').isVisible())
  await page.mouse.up()
  await page.mouse.move(700, 50)
  await wait(async () => await buttons.evaluate((el) => getComputedStyle(el).opacity) === '0')
  console.log('PASS hover, hold, release and auto-hide')

  await cells.first().getByRole('button', { name: /修改.*显示名称/ }).click()
  const input = page.getByRole('textbox', { name: '图片显示名称' })
  await input.fill('实验 A（仅展示）'); await input.press('Enter')
  assert.match(await cells.first().locator('figcaption').textContent(), /实验 A（仅展示）/)
  assert.match(await page.locator('.cmp-set').first().textContent(), /sr-x4-baseline/)
  await cells.first().getByRole('button', { name: /修改.*显示名称/ }).click()
  await input.fill('取消的标题'); await input.press('Escape')
  assert.match(await cells.first().locator('figcaption').textContent(), /实验 A（仅展示）/)
  assert.deepEqual(writes, [])
  console.log('PASS display-name edit, Enter, Escape; no result rename request')

  await vp.hover()
  await page.mouse.wheel(0, -1100)
  await wait(async () => (await range()).z > 4)
  const r = await vp.boundingBox()
  await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2)
  await page.mouse.down(); await page.mouse.move(r.x + r.width / 2 - 35, r.y + r.height / 2 - 20, { steps: 5 }); await page.mouse.up()
  const before = await range()
  for (const label of ['缩略图栏', '推理结果栏']) {
    await page.getByRole('button', { name: `折叠${label}`, exact: true }).click(); await page.waitForTimeout(650)
    const after = await range()
    for (const key of ['z', 'x', 'y', 'aspect']) assert.ok(Math.abs(after[key] - before[key]) < 0.002, `${label} ${key}: ${before[key]} -> ${after[key]}`)
  }
  for (const label of ['推理结果栏', '缩略图栏']) {
    await page.getByRole('button', { name: `展开${label}`, exact: true }).click(); await page.waitForTimeout(650)
    const after = await range()
    for (const key of ['z', 'x', 'y', 'aspect']) assert.ok(Math.abs(after[key] - before[key]) < 0.002)
  }
  console.log('PASS wheel without Ctrl, pan, fold/unfold both panels preserving range')

  // The entire grid must fit even in a short window.
  await page.setViewportSize({ width: 1600, height: 600 })
  await page.waitForTimeout(400)
  assert.ok(await page.locator('.cmp-grid').evaluate((grid) => {
    const r = grid.getBoundingClientRect()
    return grid.scrollHeight <= grid.clientHeight + 1 && [...grid.querySelectorAll('.cmp-cell')].every((cell) => {
      const c = cell.getBoundingClientRect()
      return c.top >= r.top - 1 && c.bottom <= r.bottom + 1 && c.bottom <= innerHeight
    })
  }), 'all three cells must fit without scrolling')
  console.log('PASS full grid fits short viewport without scrolling')
  await page.keyboard.press('ArrowRight')
  await page.getByRole('button', { name: '复制对比图', exact: true }).click()
  await wait(async () => /已复制|复制失败/.test(await status.textContent()))
  assert.equal(await status.textContent(), '对比图已复制')
  const png = await page.evaluate(async () => {
    const items = await navigator.clipboard.read()
    const blob = await items[0].getType('image/png'), image = await createImageBitmap(blob)
    const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height
    const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0)
    const figures = [...document.querySelectorAll('.cmp-cell')], rects = figures.map((f) => f.getBoundingClientRect())
    const left = Math.min(...rects.map((r) => r.left)), top = Math.min(...rects.map((r) => r.top))
    const matching = figures.map((f) => {
      const src = f.querySelector('canvas'), r = src.getBoundingClientRect()
      const a = src.getContext('2d').getImageData(Math.floor(src.width / 2), Math.floor(src.height / 2), 1, 1).data
      const b = ctx.getImageData(Math.floor(r.left - left + r.width / 2), Math.floor(r.top - top + r.height / 2), 1, 1).data
      return a.every((x, i) => Math.abs(x - b[i]) < 35)
    })
    return { type: blob.type, width: image.width, height: image.height, matching }
  })
  assert.ok(await cells.evaluateAll((fs) => fs.every((f) => f.querySelector('canvas').dataset.pic === f.dataset.pic)))
  assert.equal(png.type, 'image/png'); assert.ok(png.width > 500 && png.height > 200)
  assert.ok(png.matching.every(Boolean), JSON.stringify(png))
  for (const [width, height] of [[1600, 1000], [1280, 720], [1600, 600]]) {
    await page.setViewportSize({ width, height })
    for (const ids of ['1', '1,2,3', '1,2,3,4,5,6']) {
      await page.goto((process.argv[2] || 'http://127.0.0.1:5180/ui/') + '#/compare?ids=' + ids)
      await wait(async () => await cells.count() === ids.split(',').length)
      await page.waitForTimeout(400)
      assert.ok(await page.locator('.cmp-grid').evaluate((grid) => {
        const r = grid.getBoundingClientRect()
        return grid.scrollHeight <= grid.clientHeight + 1 && [...grid.querySelectorAll('.cmp-cell')].every((cell) => {
          const c = cell.getBoundingClientRect(), vp = cell.querySelector('.cmp-vp').getBoundingClientRect()
          return c.bottom <= r.bottom + 1 && c.bottom <= innerHeight && vp.width > 0 && vp.height > 0 && vp.top >= c.top && vp.bottom <= c.bottom
        })
      }), `grid must fit ${width}x${height}, ids=${ids}`)
    }
  }
  console.log('PASS 1/3/6 cells fit 1600x1000, 1280x720 and 1600x600')
  assert.deepEqual(errors, [])
  console.log('PASS real clipboard PNG includes all 3 cells:', JSON.stringify(png))
  await page.screenshot({ path: process.env.SCREENSHOT_PATH || '/tmp/compare-interactions.png' })
} catch (e) { console.log('DEBUG', await page.locator('body').innerText(), errors); await page.screenshot({ path: '/tmp/compare-debug.png' }); throw e } finally { await browser.close() }
