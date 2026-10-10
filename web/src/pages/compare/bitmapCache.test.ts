import { test } from 'node:test'
import assert from 'node:assert/strict'
import { closeSource, createLRU } from './bitmapCache.ts'
import type { Entry } from './loader.ts'

const make = (budget: number) => {
  const closed: string[] = []
  const lru = createLRU<{ name: string }>(budget, (v) => closed.push(v.name))
  return { lru, closed }
}

test('超出预算时淘汰最久未用的，并 close()', () => {
  const { lru, closed } = make(100)
  lru.set('a', { name: 'a' }, 40)
  lru.set('b', { name: 'b' }, 40)
  lru.get('a')
  lru.set('c', { name: 'c' }, 40)
  assert.deepEqual(closed, ['b'])
  assert.equal(lru.used, 80)
  assert.ok(lru.has('a') && lru.has('c') && !lru.has('b'))
  // peek 不改变顺序
  lru.peek('a')
  lru.set('d', { name: 'd' }, 40)
  assert.deepEqual(closed, ['b', 'a'])
})

test('pin 住的不淘汰，可先于 set 调用；unpin 后按预算回收', () => {
  const { lru, closed } = make(100)
  lru.pin('a')
  lru.set('a', { name: 'a' }, 60)
  lru.set('b', { name: 'b' }, 60)
  // a 被 pin，b 是刚放入的，暂时超预算
  assert.deepEqual(closed, [])
  lru.set('c', { name: 'c' }, 30)
  assert.deepEqual(closed, ['b'])
  assert.equal(lru.pinnedBytes(), 60)
  lru.pin('a')
  lru.unpin('a')
  assert.ok(lru.isPinned('a'))
  lru.set('d', { name: 'd' }, 30)
  assert.deepEqual(closed, ['b', 'c'])
  lru.unpin('a')
  assert.ok(!lru.isPinned('a'))
  lru.unpin('a')
  // 30 + 60 = 90，未超预算
  assert.deepEqual(closed, ['b', 'c'])
  lru.budget = 50
  lru.trim()
  assert.deepEqual(closed, ['b', 'c', 'a'])
})

test('同 key 替换时释放旧值，delete 与 clear 都会 close()', () => {
  const { lru, closed } = make(1000)
  const a1 = { name: 'a1' }
  lru.set('a', a1, 10)
  lru.set('a', a1, 10)
  assert.deepEqual(closed, [])
  lru.set('a', { name: 'a2' }, 20)
  assert.deepEqual(closed, ['a1'])
  assert.equal(lru.used, 20)
  lru.set('b', { name: 'b' }, 5)
  lru.delete('a')
  assert.deepEqual(closed, ['a1', 'a2'])
  lru.pin('b')
  lru.clear()
  assert.deepEqual(closed, ['a1', 'a2', 'b'])
  assert.equal(lru.used, 0)
  assert.equal(lru.size, 0)
  assert.ok(!lru.isPinned('b'))
  lru.unpin('b')
})

test('closeSource：ImageBitmap 调 close()，自建的 <img> 撤销 objectURL，与缩略图栏共用的不撤销', () => {
  const revoked: string[] = []
  const orig = URL.revokeObjectURL
  URL.revokeObjectURL = (u: string) => { revoked.push(u) }
  try {
    let closed = 0
    const entry = (bitmap: unknown) => ({ bitmap }) as unknown as Entry
    closeSource(entry({ close: () => { closed++ } }))
    closeSource(entry({ src: 'blob:own', dataset: {} }))
    closeSource(entry({ src: 'blob:shared', dataset: { shared: '1' } }))
    assert.equal(closed, 1)
    assert.deepEqual(revoked, ['blob:own'])
  } finally {
    URL.revokeObjectURL = orig
  }
})
