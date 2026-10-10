import { test } from 'node:test'
import assert from 'node:assert/strict'
import { boundedDistance, nearest, prepareSet, serialOf } from './match.ts'

function rng(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    return s / 2 ** 32
  }
}

/** PR #23 原来的实现，作为对照 */
function editDistance(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const row = [i]
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    prev = row
  }
  return prev[b.length]
}

/** PR #23 原来的规则：距离最小，相同取序号小的 */
function naiveNearest(q: string, names: string[]): number {
  let best = 0, bd = Infinity
  names.forEach((f, k) => { const d = editDistance(q, f); if (d < bd) { bd = d; best = k } })
  return best
}

const word = (r: () => number, alpha: string, min: number, max: number) =>
  Array.from({ length: min + Math.floor(r() * (max - min + 1)) }, () => alpha[Math.floor(r() * alpha.length)]).join('')

test('boundedDistance 与朴素 DP 一致（超过上界时为 max+1）', () => {
  const r = rng(1)
  for (let n = 0; n < 20000; n++) {
    const alpha = n % 3 === 0 ? 'ab' : n % 3 === 1 ? 'abcd_0123.' : '图片ab12'
    const a = word(r, alpha, 0, 14), b = r() < 0.3 ? a.slice(0, Math.floor(r() * a.length)) + word(r, alpha, 0, 3) + a.slice(Math.floor(r() * a.length)) : word(r, alpha, 0, 14)
    const max = Math.floor(r() * 10)
    const d = editDistance(a, b)
    assert.equal(boundedDistance(a, b, max), d <= max ? d : max + 1, `${a} | ${b} | ${max}`)
    assert.equal(boundedDistance(b, a, 99), d)
  }
  assert.equal(boundedDistance('', '', 0), 0)
  assert.equal(boundedDistance('', 'abc', 2), 3)
  assert.equal(boundedDistance('abc', 'abc', 0), 0)
  assert.equal(boundedDistance('kitten', 'sitting', 3), 3)
  assert.equal(boundedDistance('kitten', 'sitting', 2), 3)
})

test('serialOf 取最长的一段数字', () => {
  assert.equal(serialOf('img_0012_x4.png'), '0012')
  assert.equal(serialOf('a1b22c333'), '333')
  assert.equal(serialOf('12_34'), '12')
  assert.equal(serialOf('none'), '')
})

/** 生成一组命名相近的结果集：后缀变体、缺文件、换扩展名、打乱的名字 */
function makeSets(r: () => number, n: number): string[][] {
  const ids = Array.from({ length: n }, (_, i) => String(i * 3 + Math.floor(r() * 3)).padStart(5, '0'))
  const variants = [
    (s: string) => `${s}.png`,
    (s: string) => `${s}_sr.png`,
    (s: string) => `img_${s}.jpg`,
    (s: string) => `${s}_x4_SR.webp`,
  ]
  return variants.map((f, k) => {
    const names = ids.filter(() => r() > 0.05 * k).map(f)
    for (let i = 0; i < n / 20; i++) names.push(word(r, 'abcxyz_0123456789', 3, 12) + '.png')
    if (k === 2) names.push(names[0])
    return names.sort()
  })
}

test('nearest 与朴素算法结果完全一致（含距离相同时取序号小的）', () => {
  const r = rng(9)
  for (let round = 0; round < 6; round++) {
    const sets = makeSets(r, 120)
    const prepared = sets.map(prepareSet)
    for (let si = 0; si < sets.length; si++) {
      for (let t = 0; t < 25; t++) {
        const qi = Math.floor(r() * sets[si].length)
        const q = r() < 0.15 ? word(r, 'abc0123456789_.', 1, 16) : sets[si][qi]
        for (let ti = 0; ti < sets.length; ti++) {
          const want = naiveNearest(q, sets[ti])
          assert.equal(nearest(q, prepared[ti], qi, prepared[si]), want, `${q} → set ${ti}`)
          assert.equal(nearest(q, prepared[ti]), want)
          assert.equal(nearest(q, prepared[ti], Math.floor(r() * sets[ti].length)), want)
        }
      }
    }
  }
  // 距离相同的多个候选取序号最小的
  const tie = prepareSet(['ab', 'ac', 'ad', 'ab'])
  assert.equal(nearest('ax', tie, 3), 0)
  assert.equal(nearest('ab', tie, 3), 0)
  assert.equal(nearest('x', prepareSet([])), -1)
})

test('2 万个文件名的匹配在 100 ms 内', () => {
  const r = rng(4)
  const names = Array.from({ length: 20000 }, (_, i) => `scene_${String(i).padStart(5, '0')}_sr_x4.png`)
  const other = Array.from({ length: 20000 }, (_, i) => `scene_${String(i).padStart(5, '0')}.png`)
  const s = prepareSet(names), src = prepareSet(other)
  // 预热
  nearest('scene_00001.png', s, 1, src)
  for (let k = 0; k < 5; k++) {
    const i = Math.floor(r() * 20000)
    const t0 = performance.now()
    const got = nearest(other[i], s, (i + 7) % 20000, src)
    const dt = performance.now() - t0
    assert.equal(got, i)
    assert.ok(dt < 100, `${dt.toFixed(1)} ms`)
  }
  // 没有好种子的对抗情形
  const rand = Array.from({ length: 20000 }, () => word(r, 'abcdefghij0123456789_', 6, 20) + '.png')
  const rs = prepareSet(rand)
  const t0 = performance.now()
  nearest('zzzz_qqqq_0000.tif', rs, 0)
  assert.ok(performance.now() - t0 < 300)
})
