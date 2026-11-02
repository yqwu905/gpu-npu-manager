import test from 'node:test'
import assert from 'node:assert/strict'
import { createViewStore } from './viewStore.ts'

test('折叠、展开与不同格子尺寸保持相同的归一化显示范围', () => {
  const store = createViewStore(undefined, () => 1, () => {})
  store.zoom(4, 150, 90, 400, 300)
  store.flush()
  const before = store.forSize(400, 300)
  const after = store.forSize(800, 600)
  assert.equal(after.z, before.z)
  assert.equal(after.x / 800, before.x / 400)
  assert.equal(after.y / 600, before.y / 300)
  store.setForSize({ ...after, x: after.x - 80 }, 800, 600)
  assert.equal(store.forSize(400, 300).x, before.x - 40)
  store.zoom(1.5, 300, 180, 800, 600)
  store.flush()
  const zoomed = store.forSize(800, 600)
  assert.equal(zoomed.z, 6)
  assert.equal(zoomed.x, 300 - (300 - after.x + 80) * 1.5)
  store.reset()
  assert.deepEqual(store.forSize(400, 300), { z: 1, x: 0, y: 0 })
  store.dispose()
})
