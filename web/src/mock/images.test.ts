import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MockHttpError, mockDims, mockSource } from './images.ts'

const px = (img: { w: number; data: Uint8ClampedArray }, i: number, j: number) => {
  const o = (j * img.w + i) * 4
  return [img.data[o], img.data[o + 1], img.data[o + 2]]
}

test('尺寸：序号逢 10 为 8K，其余 4K', () => {
  assert.deepEqual(mockDims('images/0010.png'), [7680, 4320])
  assert.deepEqual(mockDims('images/0000.png'), [7680, 4320])
  assert.deepEqual(mockDims('images/0013.png'), [3840, 2160])
})

test('第 0 层瓦片逐像素精确，第 l 层取 (2^l·x, 2^l·y)', () => {
  const t = mockSource.render(3, 'images/0013.png', 'tile', { l: 0, x: 7, y: 4 })
  // 3840×2160：x=7 为最后一列（3584..3840），y=4 为最后一行（2048..2160）
  assert.equal(t.w, 256)
  assert.equal(t.h, 112)
  assert.equal(t.meta.tile, 512)
  assert.equal(t.meta.lossless, true)
  for (const [i, j] of [[0, 0], [5, 9], [255, 111]]) assert.deepEqual(px(t, i, j), mockSource.pixel(3, 'images/0013.png', 3584 + i, 2048 + j))
  const t2 = mockSource.render(3, 'images/0013.png', 'tile', { l: 2, x: 1, y: 0 })
  // 第 2 层 960×540
  assert.equal(t2.w, 448)
  assert.equal(t2.h, 512)
  assert.deepEqual(px(t2, 3, 4), mockSource.pixel(3, 'images/0013.png', (512 + 3) * 4, 4 * 4))
})

test('瓦片越界抛出 400', () => {
  for (const p of [{ l: 0, x: 8, y: 0 }, { l: 0, x: 0, y: 5 }, { l: 4, x: 0, y: 0 }, { l: -1, x: 0, y: 0 }]) {
    assert.throws(() => mockSource.render(1, 'images/0013.png', 'tile', p), (e) => e instanceof MockHttpError && e.status === 400)
  }
  // 8K 有第 4 层
  assert.equal(mockSource.render(1, 'images/0010.png', 'tile', { l: 4, x: 0, y: 0 }).w, 480)
})

test('缩略图与预览按尺寸点采样，带原图宽高', () => {
  const th = mockSource.render(2, 'images/0010.png', 'thumb')
  assert.deepEqual([th.w, th.h], [256, 144])
  assert.deepEqual([th.meta.W, th.meta.H, th.meta.lossless], [7680, 4320, false])
  assert.deepEqual(px(th, 10, 20), mockSource.pixel(2, 'images/0010.png', 300, 600))
  const pv = mockSource.render(2, 'images/0011.png', 'preview', { size: 2000 })
  assert.deepEqual([pv.w, pv.h, pv.meta.lossless], [2048, 1152, false])
  assert.equal(mockSource.render(2, 'images/0011.png', 'preview', { size: 3072 }).meta.lossless, false)
  assert.equal(mockSource.delay('preview'), 60)
})
