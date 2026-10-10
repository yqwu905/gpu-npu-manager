import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resultsApi } from './results.ts'
import { imageUrlParts } from '../pages/compare/loader.ts'

const ORDER = ['path', 'v', 'kind', 'size', 'l', 'x', 'y', 'evaluation_id']

test('imageUrl 参数顺序固定、缺省的不出现', () => {
  const u = resultsApi.imageUrl(3, 'images/a b+c#d%e/图.png', '9f2c01ab3e', 'preview', { size: 1500, evaluationId: 7 })
  assert.equal(u, '/api/results/3/image?path=images%2Fa%20b%2Bc%23d%25e%2F%E5%9B%BE.png&v=9f2c01ab3e&kind=preview&size=2048&evaluation_id=7')
  assert.equal(resultsApi.imageUrl(3, '/abs/x.png', '', 'thumb'), '/api/results/3/image?path=%2Fabs%2Fx.png&kind=thumb')
  assert.equal(resultsApi.imageUrl(3, 'x.png', '0123456789', 'full'), '/api/results/3/image?path=x.png&v=0123456789&kind=full')
  assert.equal(
    resultsApi.imageUrl(3, 'x.png', '0123456789', 'tile', { l: 2, x: 0, y: 5, evaluationId: 9 }),
    '/api/results/3/image?path=x.png&v=0123456789&kind=tile&l=2&x=0&y=5&evaluation_id=9',
  )
  for (const kind of ['thumb', 'preview', 'full', 'tile'] as const) {
    const keys = [...new URLSearchParams(resultsApi.imageUrl(1, 'p', 'abcdefabcd', kind, { size: 1, l: 1, x: 1, y: 1, evaluationId: 1 }).split('?')[1]).keys()]
    assert.deepEqual(keys, ORDER.filter((k) => keys.includes(k)), kind)
  }
})

test('同一张图逐字节相同：只对相关的参数生效，预览尺寸取档位', () => {
  const a = resultsApi.imageUrl(1, 'p.png', 'abcdefabcd', 'thumb', { size: 2048, l: 3 })
  const b = resultsApi.imageUrl(1, 'p.png', 'abcdefabcd', 'thumb')
  assert.equal(a, b)
  const t1 = resultsApi.imageUrl(1, 'p.png', 'abcdefabcd', 'tile', { y: 2, x: 1, evaluationId: 4, l: 0 })
  const t2 = resultsApi.imageUrl(1, 'p.png', 'abcdefabcd', 'tile', { l: 0, x: 1, y: 2, evaluationId: 4 })
  assert.equal(t1, t2)
  const size = (s?: number) => new URLSearchParams(resultsApi.imageUrl(1, 'p', '', 'preview', { size: s }).split('?')[1]).get('size')
  assert.deepEqual([size(), size(1), size(1024), size(1025), size(2048), size(2049), size(9000)], ['2048', '1024', '1024', '2048', '2048', '3072', '3072'])
  // evaluation_id 为 0 / 缺省时不带
  assert.equal(resultsApi.imageUrl(1, 'p', '', 'full', { evaluationId: 0 }), resultsApi.imageUrl(1, 'p', '', 'full'))
})

test('路径里的特殊字符可原样还原', () => {
  for (const p of ['a+b.png', 'a#b.png', '100%.png', 'with space.png', '中文/图片 1.png', 'C:\\data\\x.tif', '/abs/p?q=1&r=2.png']) {
    const u = resultsApi.imageUrl(12, p, 'abcdefabcd', 'tile', { l: 1, x: 2, y: 3 })
    assert.deepEqual(imageUrlParts(u), { id: 12, path: p, kind: 'tile', size: undefined, l: 1, x: 2, y: 3 })
    assert.equal(new URL(u, 'http://h').searchParams.get('path'), p)
  }
  assert.equal(imageUrlParts('/api/results/1/file?path=x'), null)
})
