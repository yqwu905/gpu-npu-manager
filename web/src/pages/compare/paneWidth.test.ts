import { test } from 'node:test'
import assert from 'node:assert/strict'
import { clampW, keyWidth, loadW, saveW, type Store } from './paneWidth.ts'

const mem = (): Store & { m: Map<string, string> } => {
  const m = new Map<string, string>()
  return { m, getItem: (k) => m.get(k) ?? null, setItem: (k, v) => { m.set(k, v) }, removeItem: (k) => { m.delete(k) } }
}

test('宽度夹在范围内，范围倒置时取最小值', () => {
  assert.equal(clampW(100, { min: 180, max: 480 }), 180)
  assert.equal(clampW(600, { min: 180, max: 480 }), 480)
  assert.equal(clampW(300.4, { min: 180, max: 480 }), 300)
  assert.equal(clampW(300, { min: 180, max: 120 }), 180)
})

test('键盘调整步长与 Home / End', () => {
  const b = { min: 180, max: 480 }
  assert.equal(keyWidth('ArrowLeft', false, 236, b), 220)
  assert.equal(keyWidth('ArrowRight', true, 236, b), 300)
  assert.equal(keyWidth('ArrowRight', true, 460, b), 480)
  assert.equal(keyWidth('Home', false, 236, b), 180)
  assert.equal(keyWidth('End', false, 236, b), 480)
  assert.equal(keyWidth('Enter', false, 236, b), null)
})

test('保存、读取与恢复默认', () => {
  const s = mem()
  assert.equal(loadW('k', s), null)
  saveW('k', 312.6, s)
  assert.equal(s.m.get('k'), '313')
  assert.equal(loadW('k', s), 313)
  saveW('k', null, s)
  assert.equal(loadW('k', s), null)
  s.setItem('k', 'abc')
  assert.equal(loadW('k', s), null)
  s.setItem('k', '-5')
  assert.equal(loadW('k', s), null)
  assert.equal(loadW('k', null), null)
  saveW('k', 200, null)
})
