import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ImageEntry, ImageList } from '../../api/types'
import { resultsApi } from '../../api/results.ts'
import {
  F_KNOWN, F_NATIVE, MAX_CACHED, baseName, buildIndex, clearIndexCache, indexOfPath, loadIndex, noteMeta, peekIndex, pinIndexes, remapIndex, revalidate,
} from './imageIndex.ts'
import { loader } from './loader.ts'

const list = (files: ImageEntry[]): ImageList => ({ total: files.length, missing: files.filter((f) => f[1] === -1).length, skipped: 0, truncated: false, source: 'agent', files })

test('建索引：byName 取第一次出现的序号，缺失与未知大小', () => {
  const ix = buildIndex(1, '"e1"', list([
    ['a/x.png', 10, '0123456789'], ['b/x.png', -1, ''], ['C:\\d\\y.png', null, ''], ['/abs/z.png', 5, 'abcdefabcd'],
  ]))
  assert.equal(ix.n, 4)
  assert.deepEqual(ix.names, ['x.png', 'x.png', 'y.png', 'z.png'])
  assert.equal(ix.byName.get('x.png'), 0)
  assert.equal(ix.byName.get('y.png'), 2)
  assert.equal(ix.sizes[0], 10)
  assert.equal(ix.sizes[1], -1)
  assert.ok(Number.isNaN(ix.sizes[2]))
  assert.deepEqual(ix.vs, ['0123456789', '', '', 'abcdefabcd'])
  assert.equal(ix.missing, 1)
  assert.equal(ix.dims.length, 8)
  assert.equal(baseName('dir/'), '')
  assert.equal(indexOfPath(ix, '/abs/z.png'), 3)
  assert.equal(indexOfPath(ix, 'nope'), -1)
  noteMeta(ix, 3, { W: 640, H: 480, format: 'png', native: true, lossless: false, normalized: false, tile: 0 })
  assert.deepEqual([ix.dims[6], ix.dims[7], ix.flags[3]], [640, 480, F_KNOWN | F_NATIVE])
  // 没有任何图片头的响应不记
  noteMeta(ix, 2, { W: 0, H: 0, format: '', native: false, lossless: false, normalized: false, tile: 0 })
  assert.equal(ix.flags[2], 0)
})

test('列表变化后按路径换算位置', () => {
  const prev = buildIndex(1, 'a', list([['a.png', 1, ''], ['c.png', 1, ''], ['e.png', 1, ''], ['g.png', 1, '']]))
  const next = buildIndex(1, 'b', list([['a.png', 1, ''], ['b.png', 1, ''], ['c.png', 1, ''], ['f.png', 1, ''], ['g.png', 1, '']]))
  assert.equal(remapIndex(prev, next, 0), 0)
  assert.equal(remapIndex(prev, next, 1), 2)
  // e.png 没了：取排序上紧随其后的 f.png
  assert.equal(remapIndex(prev, next, 2), 3)
  assert.equal(remapIndex(prev, next, 3), 4)
  assert.equal(remapIndex(prev, buildIndex(1, 'c', list([])), 2), 0)
  assert.equal(remapIndex(prev, buildIndex(1, 'c', list([['a.png', 1, '']])), 3), 0)
  assert.equal(remapIndex(prev, next, 99), 4)
})

/** 用假的 fetch 模拟 /images 与 /image */
function fakeServer() {
  const lists = new Map<number, { etag: string; files: ImageEntry[] }>()
  const seen: { url: string; inm: string | null }[] = []
  globalThis.fetch = (async (input: string, init?: RequestInit) => {
    const u = String(input), inm = new Headers(init?.headers).get('If-None-Match')
    seen.push({ url: u, inm })
    const m = /\/api\/results\/(\d+)\/images$/.exec(u)
    if (m) {
      const l = lists.get(Number(m[1]))
      if (!l) return new Response(JSON.stringify({ detail: '结果集不存在' }), { status: 404 })
      if (inm === l.etag) return new Response(null, { status: 304, headers: { ETag: l.etag } })
      return new Response(JSON.stringify(list(l.files)), { status: 200, headers: { ETag: l.etag, 'Content-Type': 'application/json' } })
    }
    return new Response('img', { status: 200, headers: { 'X-Image-Width': '800', 'X-Image-Height': '600', 'X-Image-Format': 'jpeg', 'X-Image-Native': '1' } })
  }) as typeof fetch
  return { lists, seen }
}

test('模块缓存、带 If-None-Match 的重新验证、尺寸由图片响应补齐', async () => {
  clearIndexCache()
  const { lists, seen } = fakeServer()
  lists.set(1, { etag: '"v1"', files: [['a.png', 1, 'aaaaaaaaaa'], ['b.png', 2, 'bbbbbbbbbb']] })
  const [i1, i2] = await Promise.all([loadIndex(1), loadIndex(1)])
  assert.equal(i1, i2)
  assert.equal(seen.length, 1)
  assert.equal(peekIndex(1), i1)
  assert.equal(i1.etag, '"v1"')

  // 任一图片响应带回尺寸
  const h = loader.acquire(resultsApi.imageUrl(1, 'b.png', 'bbbbbbbbbb', 'thumb'), 'thumb', 3, 'blob')
  await h.promise
  h.release()
  assert.deepEqual([i1.dims[2], i1.dims[3], i1.flags[1]], [800, 600, F_KNOWN | F_NATIVE])

  // 未变化：304，仍是同一个对象
  assert.equal(await revalidate(1), i1)
  assert.equal(seen.at(-1)!.inm, '"v1"')

  // 变化：新对象，版本号相同的沿用尺寸，缓存换成新的
  lists.set(1, { etag: '"v2"', files: [['0.png', 1, 'cccccccccc'], ['a.png', 1, 'aaaaaaaaaa'], ['b.png', 2, 'bbbbbbbbbb']] })
  const i3 = await revalidate(1)
  assert.notEqual(i3, i1)
  assert.equal(i3.n, 3)
  assert.deepEqual([i3.dims[4], i3.dims[5]], [800, 600])
  assert.equal(peekIndex(1), i3)
  assert.equal(await loadIndex(1), i3)
  assert.equal(remapIndex(i1, i3, 1), 2)

  // 失败的不缓存
  await assert.rejects(loadIndex(99))
  assert.equal(peekIndex(99), null)
})

test('模块缓存只保留最近 12 个结果集', async () => {
  clearIndexCache()
  const { lists } = fakeServer()
  for (let id = 1; id <= MAX_CACHED + 1; id++) lists.set(id, { etag: `"${id}"`, files: [[`${id}.png`, 1, '']] })
  for (let id = 1; id <= MAX_CACHED; id++) await loadIndex(id)
  // 用一下 1，淘汰的就是 2
  await loadIndex(1)
  await loadIndex(MAX_CACHED + 1)
  assert.ok(peekIndex(1))
  assert.equal(peekIndex(2), null)
  assert.ok(peekIndex(MAX_CACHED + 1))
})

test('正在显示的结果集不被淘汰：选了超过 12 个、或保留一个而增删其他的', async () => {
  clearIndexCache()
  const { lists, seen } = fakeServer()
  for (let id = 1; id <= 30; id++) lists.set(id, { etag: `"${id}"`, files: [[`${id}.png`, 1, '']] })
  // 同时显示 14 个
  const shown = Array.from({ length: 14 }, (_, k) => k + 1)
  let unpin = pinIndexes(shown)
  for (const id of shown) await loadIndex(id)
  assert.ok(shown.every((id) => peekIndex(id)))
  // 换成 1 和 15..26：与 React 一样先解除旧的再 pin 新的，中间没有加载，不会淘汰
  const next = [1, ...Array.from({ length: 12 }, (_, k) => k + 15)]
  unpin()
  unpin = pinIndexes(next)
  for (const id of next) await loadIndex(id)
  assert.ok(next.every((id) => peekIndex(id)))
  const fetched = seen.length
  for (const id of next) await loadIndex(id)
  assert.equal(seen.length, fetched)
  unpin()
  // 不再显示后按最近使用淘汰
  await loadIndex(27)
  assert.equal(peekIndex(1), null)
})
