import { test } from 'node:test'
import assert from 'node:assert/strict'
import { atEnd, atStart, clampIdx, stepAll, syncAll } from './nav.ts'

// 改版前 CompareImages.tsx 里的写法，作为对照
const oldStep = (c: Record<number, number>, ids: number[], lens: Record<number, number | undefined>, d: number) =>
  ({ ...c, ...Object.fromEntries(ids.map((x) => [x, Math.min(Math.max(0, (lens[x] ?? 0) - 1), Math.max(0, Math.min(c[x] ?? 0, (lens[x] ?? 1) - 1) + d))])) })
const oldSync = (c: Record<number, number>, ids: number[], lens: Record<number, number>, i: number) =>
  ({ ...c, ...Object.fromEntries(ids.map((x) => [x, Math.min(i, Math.max(0, (lens[x] ?? 0) - 1))])) })

test('前进后退、按序号同步与改版前一致', () => {
  let r = 17
  const rnd = (n: number) => (r = (r * 1103515245 + 12345) % 2147483648) % n
  for (let k = 0; k < 2000; k++) {
    const ids = [1, 2, 3, 4].slice(0, 1 + rnd(4))
    const lens: Record<number, number> = {}
    const cur: Record<number, number> = {}
    for (const id of ids) {
      if (rnd(5)) lens[id] = rnd(6)
      if (rnd(3)) cur[id] = rnd(8)
    }
    const d = rnd(2) ? 1 : -1
    // 未加载的结果集：旧代码按长度 1 夹紧当前位置，结果相同
    assert.deepEqual(stepAll(cur, ids, lens, d), oldStep(cur, ids, lens, d))
    const i = rnd(8)
    assert.deepEqual(syncAll(cur, ids, lens, i), oldSync(cur, ids, lens, i))
  }
})

test('clampIdx 与到头判断', () => {
  assert.equal(clampIdx(undefined, 5), 0)
  assert.equal(clampIdx(9, 5), 4)
  assert.equal(clampIdx(3, 0), 0)
  assert.ok(atStart({}, [1, 2], { 1: 3, 2: 0 }))
  assert.ok(!atStart({ 1: 1 }, [1, 2], { 1: 3 }))
  assert.ok(atEnd({ 1: 2 }, [1, 2], { 1: 3, 2: 1 }))
  assert.ok(atEnd({ 1: 9 }, [1], { 1: 3 }))
  assert.ok(!atEnd({ 1: 1 }, [1, 2], { 1: 3, 2: 1 }))
})
