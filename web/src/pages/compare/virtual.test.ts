import { test } from 'node:test'
import assert from 'node:assert/strict'
import { MAX_SCROLL_PX, ROW_H, follow, rangeOf, sameRange, vmap } from './virtual.ts'

test('rangeOf 边界情况', () => {
  assert.deepEqual(rangeOf(0, 500, 0), { first: 0, last: -1, start: 0, end: -1 })
  assert.deepEqual(rangeOf(0, 500, 1), { first: 0, last: 0, start: 0, end: 0 })
  // 视口 500 / 行高 112 → 0..4 可见
  assert.deepEqual(rangeOf(0, 500, 100), { first: 0, last: 4, start: 0, end: 8 })
  // 恰好对齐行边界时不多算一行
  assert.deepEqual(rangeOf(112, 224, 100), { first: 1, last: 2, start: 0, end: 6 })
  // 列表末尾
  assert.deepEqual(rangeOf(100 * ROW_H - 500, 500, 100), { first: 95, last: 99, start: 91, end: 99 })
  // 滚过头（弹性滚动）、负的 scrollTop
  assert.deepEqual(rangeOf(1e9, 500, 100), { first: 99, last: 99, start: 95, end: 99 })
  assert.deepEqual(rangeOf(-300, 500, 100), { first: 0, last: 1, start: 0, end: 5 })
  assert.deepEqual(rangeOf(0, 0, 10), { first: 0, last: 0, start: 0, end: 4 })
  assert.deepEqual(rangeOf(0, 500, 100, 112, 0), { first: 0, last: 4, start: 0, end: 4 })
  assert.ok(sameRange(rangeOf(5, 500, 100), rangeOf(6, 500, 100)))
  assert.ok(!sameRange(rangeOf(0, 500, 100), rangeOf(120, 500, 100)))
})

test('未超上限时不缩放', () => {
  const m = vmap(20000, 800)
  assert.equal(m.scaled, false)
  assert.equal(m.height, 20000 * ROW_H)
  assert.equal(m.toVirtual(12345), 12345)
  assert.equal(m.offset(12345), 0)
})

test('超高列表的缩放映射可往返，两端对齐', () => {
  const n = 200000, viewH = 800
  const m = vmap(n, viewH)
  assert.equal(m.scaled, true)
  assert.equal(m.height, MAX_SCROLL_PX)
  assert.equal(m.total, n * ROW_H)
  assert.equal(m.toVirtual(0), 0)
  // 真实滚到底时虚拟也到底，最后一行可见
  const end = m.toVirtual(m.height - viewH)
  assert.ok(Math.abs(end - (m.total - viewH)) < 1e-6)
  assert.equal(rangeOf(end, viewH, n).last, n - 1)
  for (const p of [0, 1, 777, 5e6, m.height - viewH]) assert.ok(Math.abs(m.toPhysical(m.toVirtual(p)) - p) < 1e-6)
  // 行在占位元素中的位置 = i × 行高 + offset，落在视口内
  const p = 7e6, top = m.toVirtual(p), r = rangeOf(top, viewH, n)
  const y = r.first * ROW_H + m.offset(p)
  assert.ok(y <= p && y + ROW_H > p)
})

test('follow：只在当前行不完整可见时滚动', () => {
  assert.equal(follow(3, 0, 500), null)
  assert.equal(follow(4, 0, 500), 4 * ROW_H + ROW_H - 500)
  assert.equal(follow(2, 5 * ROW_H, 500), 2 * ROW_H)
  assert.equal(follow(5, 5 * ROW_H, 500), null)
  // 视口比一行矮：行顶对齐
  assert.equal(follow(7, 0, 50), 7 * ROW_H)
  // 缩放映射下往返后仍落在目标行
  const m = vmap(200_000, 600)
  const t = follow(150_000, m.toVirtual(0), 600)!
  const r = rangeOf(m.toVirtual(m.toPhysical(t)), 600, 200_000)
  assert.ok(r.first <= 150_000 && r.last >= 150_000)
})
