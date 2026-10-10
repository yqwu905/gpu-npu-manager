import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_ZOOM, TILE, clampView, fit, levelDims, levelFor, maxLevel, planLossless, previewDims, previewSize, scaleK, snapRect, tileCount, tileRect,
  visibleRect, visibleTiles, zoomAt, type View,
} from './geometry.ts'

// 固定种子的伪随机数，保证可复现
function rng(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    return s / 2 ** 32
  }
}

test('previewSize 取不小于格子长边设备像素的档位', () => {
  assert.equal(previewSize(500, 300, 1), 1024)
  assert.equal(previewSize(512, 300, 2), 1024)
  assert.equal(previewSize(600, 300, 2), 2048)
  assert.equal(previewSize(1100, 800, 2), 3072)
  assert.equal(previewSize(2000, 1000, 2), 3072)
  assert.equal(previewSize(1024, 10, 1), 1024)
  assert.equal(previewSize(1025, 10, 1), 2048)
})

test('previewDims 与 Pillow thumbnail 一致', () => {
  // 由 Pillow 12 Image.thumbnail 实测
  const cases: [number, number, number, number, number][] = [
    [3840, 2160, 2048, 2048, 1152], [1001, 999, 256, 256, 255], [999, 1001, 256, 255, 256], [7680, 4320, 256, 256, 144],
    [4320, 7680, 1024, 576, 1024], [1300, 700, 256, 256, 138], [333, 1000, 256, 85, 256], [3, 1000, 256, 1, 256],
    [1000, 3, 256, 256, 1], [2048, 1152, 2048, 2048, 1152], [5000, 4999, 3072, 3072, 3071],
  ]
  for (const [W, H, s, w, h] of cases) assert.deepEqual(previewDims(W, H, s), [w, h], `${W}x${H}@${s}`)
})

test('levelFor 阈值', () => {
  assert.equal(levelFor(1, 4), 0)
  assert.equal(levelFor(3, 4), 0)
  assert.equal(levelFor(0.99, 4), 0)
  assert.equal(levelFor(0.5, 4), 1)
  assert.equal(levelFor(0.49, 4), 1)
  assert.equal(levelFor(0.26, 4), 1)
  assert.equal(levelFor(0.25, 4), 2)
  assert.equal(levelFor(0.01, 3), 3)
  assert.equal(levelFor(0.1, 0), 0)
  // 选出的层每个像素不超过一个设备像素
  for (let k = 0.02; k < 1; k += 0.013) assert.ok(2 ** levelFor(k, 10) * k <= 1 + 1e-12)
})

test('levelDims / maxLevel 与 Pillow reduce 一样向上取整', () => {
  assert.deepEqual(levelDims(1001, 999, 2), [251, 250])
  assert.deepEqual(levelDims(7680, 4320, 4), [480, 270])
  assert.deepEqual(levelDims(1300, 700, 0), [1300, 700])
  assert.equal(maxLevel(512, 300), 0)
  assert.equal(maxLevel(100, 50), 0)
  assert.equal(maxLevel(513, 1), 1)
  assert.equal(maxLevel(1024, 10), 1)
  assert.equal(maxLevel(1025, 10), 2)
  assert.equal(maxLevel(7680, 4320), 4)
  // 最高层只有一块瓦片
  for (const [W, H] of [[7680, 4320], [1025, 3], [513, 513], [100000, 7]]) assert.deepEqual(tileCount(W, H, maxLevel(W, H)), [1, 1])
  assert.deepEqual(tileCount(1300, 700, 0), [3, 2])
  assert.deepEqual(tileRect(1300, 700, 0, 2, 1), [1024, 512, 1300, 700])
})

const randomView = (r: () => number, cw: number, ch: number): View => {
  const z = 1 + r() * (MAX_ZOOM - 1)
  return clampView(z, -r() * cw * z, -r() * ch * z, cw, ch)
}

test('visibleTiles 恰好覆盖视口，外加 ring 圈', () => {
  const r = rng(7)
  for (let n = 0; n < 500; n++) {
    const W = 1 + Math.floor(r() * 9000), H = 1 + Math.floor(r() * 6000)
    const cw = 100 + r() * 900, ch = 100 + r() * 700
    const v = n % 5 === 0 ? { z: 1, x: 0, y: 0 } : randomView(r, cw, ch)
    const ft = fit(W, H, cw, ch)
    const l = Math.floor(r() * (maxLevel(W, H) + 1))
    const vr = visibleRect(v, ft, cw, ch, W, H)
    const tiles = visibleTiles(W, H, l, v, ft, cw, ch, 0)
    if (!vr) { assert.equal(tiles.length, 0); continue }
    const s = TILE * 2 ** l
    // 每块都与可见范围相交
    for (const t of tiles) {
      const [x0, y0, x1, y1] = tileRect(W, H, l, t.x, t.y)
      assert.ok(x0 < vr[2] && x1 > vr[0] && y0 < vr[3] && y1 > vr[1], 'tile intersects view')
      assert.equal(t.ring, false)
    }
    // 可见范围内的点都落在某块里
    for (let k = 0; k < 20; k++) {
      const u = vr[0] + r() * (vr[2] - vr[0]), w = vr[1] + r() * (vr[3] - vr[1])
      const tx = Math.min(Math.floor(u / s), tileCount(W, H, l)[0] - 1), ty = Math.min(Math.floor(w / s), tileCount(W, H, l)[1] - 1)
      assert.ok(tiles.some((t) => t.x === tx && t.y === ty), 'point covered')
    }
    // ring=1 时多出的恰好是外面一圈（截到图片内）
    const ringed = visibleTiles(W, H, l, v, ft, cw, ch, 1)
    const xs = tiles.map((t) => t.x), ys = tiles.map((t) => t.y)
    const [nx, ny] = tileCount(W, H, l)
    const ex0 = Math.max(0, Math.min(...xs) - 1), ex1 = Math.min(nx - 1, Math.max(...xs) + 1)
    const ey0 = Math.max(0, Math.min(...ys) - 1), ey1 = Math.min(ny - 1, Math.max(...ys) + 1)
    assert.equal(ringed.length, (ex1 - ex0 + 1) * (ey1 - ey0 + 1))
    assert.equal(ringed.filter((t) => !t.ring).length, tiles.length)
    // 内圈排在外圈前面
    const firstRing = ringed.findIndex((t) => t.ring)
    if (firstRing >= 0) assert.ok(ringed.slice(firstRing).every((t) => t.ring))
  }
})

test('相邻瓦片对齐后的设备像素矩形共用边，整张图与预览的矩形相同', () => {
  const r = rng(11)
  for (const dpr of [1, 1.25, 2]) {
    for (let n = 0; n < 1000; n++) {
      const W = 300 + Math.floor(r() * 8000), H = 300 + Math.floor(r() * 5000)
      const cw = 200 + r() * 800, ch = 150 + r() * 600
      const v = randomView(r, cw, ch), ft = fit(W, H, cw, ch)
      const l = Math.floor(r() * (maxLevel(W, H) + 1))
      const [nx, ny] = tileCount(W, H, l)
      const whole = snapRect(v, ft, dpr, 0, 0, W, H)
      const rect = (x: number, y: number) => { const [x0, y0, x1, y1] = tileRect(W, H, l, x, y); return snapRect(v, ft, dpr, x0, y0, x1, y1) }
      for (let y = 0; y < ny; y++) {
        for (let x = 0; x < nx; x++) {
          const a = rect(x, y)
          if (x + 1 < nx) assert.equal(a[0] + a[2], rect(x + 1, y)[0])
          if (y + 1 < ny) assert.equal(a[1] + a[3], rect(x, y + 1)[1])
        }
      }
      // 瓦片拼起来正好是整张图的矩形；预览、缩略图都画进这个矩形
      const tl = rect(0, 0), br = rect(nx - 1, ny - 1)
      assert.deepEqual([tl[0], tl[1], br[0] + br[2] - tl[0], br[1] + br[3] - tl[1]], whole)
    }
  }
})

test('contain 适配只取决于原图宽高，预览与原图的矩形一致', () => {
  const r = rng(3)
  for (let n = 0; n < 200; n++) {
    const W = 100 + Math.floor(r() * 8000), H = 100 + Math.floor(r() * 5000), cw = 200 + r() * 800, ch = 150 + r() * 600
    const ft = fit(W, H, cw, ch)
    // 图片居中且贴住一条边
    assert.ok(Math.abs(ft.ox * 2 + W * ft.f - cw) < 1e-6 && Math.abs(ft.oy * 2 + H * ft.f - ch) < 1e-6)
    assert.ok(Math.abs(ft.ox) < 1e-6 || Math.abs(ft.oy) < 1e-6)
    // 预览自己的宽高比与原图有取整误差，按预览宽高适配会偏移；按原图宽高画则完全一致
    const [pw, ph] = previewDims(W, H, 1024)
    const fp = fit(pw, ph, cw, ch)
    assert.ok(Math.abs(fp.ox - ft.ox) < 2 && Math.abs(fp.oy - ft.oy) < 2)
  }
  assert.deepEqual(fit(0, 10, 100, 100), { f: 0, ox: 50, oy: 50 })
})

test('clampView / zoomAt 不变式', () => {
  const r = rng(5)
  for (let n = 0; n < 2000; n++) {
    const w = 100 + r() * 900, h = 100 + r() * 700
    const v0 = randomView(r, w, h)
    const cx = r() * w, cy = r() * h, factor = Math.exp((r() - 0.5) * 3)
    const v = zoomAt(v0, factor, cx, cy, w, h)
    assert.ok(v.z >= 1 && v.z <= MAX_ZOOM)
    // 图片层仍铺满视口
    assert.ok(v.x <= 1e-9 && v.x + w * v.z >= w - 1e-9 && v.y <= 1e-9 && v.y + h * v.z >= h - 1e-9)
    // 未被夹紧时光标下的点不动
    const ux = (cx - v0.x) / v0.z, uy = (cy - v0.y) / v0.z
    const free = clampView(v.z, cx - ux * v.z, cy - uy * v.z, w, h)
    if (free.x === cx - ux * v.z && free.y === cy - uy * v.z) {
      assert.ok(Math.abs(v.x + ux * v.z - cx) < 1e-6 && Math.abs(v.y + uy * v.z - cy) < 1e-6)
    }
  }
  assert.deepEqual(zoomAt({ z: 1, x: 0, y: 0 }, 0.5, 10, 10, 100, 100), { z: 1, x: 0, y: 0 })
  assert.equal(zoomAt({ z: 10, x: 0, y: 0 }, 100, 0, 0, 100, 100).z, MAX_ZOOM)
})

test('planLossless 选择无损来源', () => {
  const base = { W: 7680, H: 4320, size: null, native: true, previewLossless: false, previewLong: 2048, cw: 800, ch: 450, dpr: 1 }
  const ft = fit(7680, 4320, 800, 450)
  // 适应窗口时预览足够
  assert.deepEqual(planLossless({ ...base, v: { z: 1, x: 0, y: 0 }, ft }), { kind: 'none' })
  // 放大后大小未知 → 瓦片
  const v = zoomAt({ z: 1, x: 0, y: 0 }, 4, 400, 225, 800, 450)
  const p = planLossless({ ...base, v, ft })
  assert.equal(p.kind, 'tiles')
  if (p.kind === 'tiles') {
    assert.equal(p.level, levelFor(v.z * ft.f, maxLevel(7680, 4320)))
    assert.ok(p.tiles.length > 0 && p.tiles.some((t) => t.ring))
  }
  // 小的 native 原图 → 整张
  const small = { ...base, W: 3000, H: 2000, size: 5_000_000, ft: fit(3000, 2000, 800, 450) }
  assert.deepEqual(planLossless({ ...small, v }), { kind: 'full' })
  // 整张原图要缩小超过 2 倍时（k < 0.5）用瓦片层级，缩小不超过 2 倍，不出摩尔纹
  const v15 = zoomAt({ z: 1, x: 0, y: 0 }, 1.5, 400, 225, 800, 450)
  const p15 = planLossless({ ...small, v: v15 })
  assert.ok(scaleK(v15, small.ft, 1) < 0.5 && p15.kind === 'tiles' && p15.level === 1)
  assert.equal(planLossless({ ...small, size: 13 * 1024 * 1024, v }).kind, 'tiles')
  assert.equal(planLossless({ ...small, native: false, v }).kind, 'tiles')
  assert.equal(planLossless({ ...small, size: NaN, v }).kind, 'tiles')
  // 预览本身无损
  assert.deepEqual(planLossless({ ...small, previewLossless: true, v }), { kind: 'none' })
  // 高 DPR 下 z=1 也可能需要无损层
  assert.equal(planLossless({ ...base, dpr: 3, previewLong: 2048, cw: 1000, ch: 563, ft: fit(7680, 4320, 1000, 563), v: { z: 1, x: 0, y: 0 } }).kind, 'tiles')
})
