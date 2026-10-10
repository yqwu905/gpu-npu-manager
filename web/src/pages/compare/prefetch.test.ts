import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ImageEntry } from '../../api/types'
import { buildIndex } from './imageIndex.ts'
import type { Handle, Lane, As } from './loader.ts'
import { createHoverQueue, createPrefetcher, neighbourCount, planPrefetch } from './prefetch.ts'

const MB = 1024 * 1024
const ix = (id: number, n: number, missing: number[] = []) =>
  buildIndex(id, 'e', { total: n, missing: 0, skipped: 0, truncated: false, source: 'agent', files: Array.from({ length: n }, (_, i): ImageEntry => [`${i}.png`, missing.includes(i) ? -1 : 100, '']) })
const kindOf = (u: string) => new URLSearchParams(u.split('?')[1]).get('kind')
const pathOf = (u: string) => new URLSearchParams(u.split('?')[1]).get('path')

test('预取窗口：+dir 为 P2，−dir、+2dir 为 P4，缩略图 +dir；跳过越界与缺失', () => {
  const a = ix(1, 10), b = ix(2, 10, [6])
  const w = planPrefetch({ sets: [{ ix: a, i: 5 }, { ix: b, i: 5 }], dir: 1, size: 2048, budget: 1024 * MB })
  const of = (id: number) => w.filter((x) => x.url.includes(`/results/${id}/`)).map((x) => [kindOf(x.url), pathOf(x.url), x.prio, x.lane])
  assert.deepEqual(of(1), [['preview', '6.png', 2, 'prefetch'], ['preview', '4.png', 4, 'prefetch'], ['preview', '7.png', 4, 'prefetch'], ['thumb', '6.png', 2, 'prefetch']])
  assert.deepEqual(of(2), [['preview', '4.png', 4, 'prefetch'], ['preview', '7.png', 4, 'prefetch']])
  // 反方向、在开头
  const back = planPrefetch({ sets: [{ ix: a, i: 0 }], dir: -1, size: 2048, budget: 1024 * MB })
  assert.deepEqual(back.map((x) => pathOf(x.url)), ['1.png'])
  assert.ok(w.every((x) => x.as === 'bitmap'))
})

test('结果集多、预算小时缩小预取半径', () => {
  const pb = 2048 * 1152 * 4
  assert.equal(neighbourCount(6, pb, 512 * MB), 3)
  assert.equal(neighbourCount(9, pb, 384 * MB), 1)
  assert.equal(neighbourCount(30, pb, 384 * MB), 0)
  const sets = Array.from({ length: 9 }, (_, k) => ({ ix: ix(k + 1, 10), i: 5 }))
  const w = planPrefetch({ sets, dir: 1, size: 2048, budget: 384 * MB })
  assert.equal(w.filter((x) => kindOf(x.url) === 'preview').length, 9)
})

test('放大时预取 +dir 同一视口的无损瓦片', () => {
  const a = ix(1, 10)
  a.dims[12] = 7680
  a.dims[13] = 4320
  const zoom = { v: { z: 4, x: -1200, y: -700 }, cw: 800, ch: 450, dpr: 1 }
  const w = planPrefetch({ sets: [{ ix: a, i: 5 }], dir: 1, size: 2048, budget: 1024 * MB, zoom })
  const tiles = w.filter((x) => kindOf(x.url) === 'tile')
  assert.ok(tiles.length > 0)
  assert.ok(tiles.every((x, k) => x.prio === 4 + k / 100 && pathOf(x.url) === '6.png'))
  // 预算已满时不预取无损层
  const full = planPrefetch({ sets: [{ ix: a, i: 5 }], dir: 1, size: 2048, budget: 1024 * MB, used: 1024 * MB, zoom })
  assert.equal(full.filter((x) => kindOf(x.url) === 'tile').length, 0)
})

function fakeAcquire() {
  const log: string[] = []
  const acquire = (url: string, _lane: Lane, prio: number, as: As): Handle => {
    log.push(`acquire ${url} ${prio}`)
    const h = {
      url, as, promise: new Promise<never>(() => {}), entry: null, error: null, progress: 0, released: false,
      setPrio: (p: number) => log.push(`prio ${url} ${p}`),
      release: () => log.push(`release ${url}`),
    }
    return h
  }
  return { log, acquire }
}

test('与已持有的句柄做差：先 acquire 新的，再 release 不要的', () => {
  const { log, acquire } = fakeAcquire()
  const p = createPrefetcher(acquire)
  p.update([{ url: 'a', lane: 'prefetch', prio: 2, as: 'bitmap' }, { url: 'b', lane: 'prefetch', prio: 4, as: 'bitmap' }])
  p.update([{ url: 'b', lane: 'prefetch', prio: 2, as: 'bitmap' }, { url: 'c', lane: 'prefetch', prio: 4, as: 'bitmap' }, { url: 'c', lane: 'prefetch', prio: 2, as: 'bitmap' }])
  assert.deepEqual(log, ['acquire a 2', 'acquire b 4', 'prio b 2', 'acquire c 4', 'prio c 2', 'release a'])
  assert.equal(p.size, 2)
  p.clear()
  assert.deepEqual(log.slice(-2), ['release b', 'release c'])
  assert.equal(p.size, 0)

  const hover = fakeAcquire()
  const q = createHoverQueue(2, hover.acquire)
  q.add('x')
  q.add('x')
  q.add('y')
  q.add('z')
  assert.deepEqual(hover.log, ['acquire x 2', 'acquire y 2', 'acquire z 2', 'release x'])
})
