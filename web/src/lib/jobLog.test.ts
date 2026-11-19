import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  applyAppend,
  applyPrepend,
  applyTail,
  emptyLogState,
  formatLogBytes,
  loadedBytes,
  logText,
  needsTailJump,
  trimLogState,
} from './jobLog.ts'
import { mockJobs } from '../mock/jobs.ts'

test('applyTail 优先展示末尾区间并记录未加载偏移量', () => {
  const s = applyTail({ offset: 900, next_offset: 1000, size: 1000, data: 'tail-lines\n' })
  assert.equal(s.initialized, true)
  assert.equal(s.headOffset, 900)
  assert.equal(s.tailOffset, 1000)
  assert.equal(s.size, 1000)
  assert.equal(loadedBytes(s), 100)
  assert.equal(logText(s), 'tail-lines\n')

  const empty = applyTail({ offset: 0, next_offset: 0, size: 0, data: '' })
  assert.equal(empty.initialized, true)
  assert.equal(empty.chunks.length, 0)
  assert.equal(logText(empty), '')
})

test('applyAppend 增量追加、合并相邻小块并在开启跟随时按块裁剪头部', () => {
  let s = applyTail({ offset: 0, next_offset: 0, size: 0, data: '' })
  // 相邻小块会合并，避免长时间运行产生过多碎片
  s = applyAppend(s, { offset: 0, next_offset: 5, size: 5, data: 'hello' })
  s = applyAppend(s, { offset: 5, next_offset: 11, size: 11, data: ' world' })
  assert.equal(s.chunks.length, 1)
  assert.equal(logText(s), 'hello world')
  assert.equal(s.headOffset, 0)
  assert.equal(s.tailOffset, 11)

  // 无新内容时不重复创建块
  const same = applyAppend(s, { offset: 11, next_offset: 11, size: 11, data: '' })
  assert.equal(same, s)

  // 超过 MERGE_CHUNK_BYTES 后产生独立块，开启裁剪时从头部按块淘汰并保持精确 headOffset
  const big = 'a'.repeat(300 * 1024)
  let w = applyTail({ offset: 0, next_offset: big.length, size: big.length, data: big })
  const b2 = 'b'.repeat(300 * 1024)
  w = applyAppend(w, { offset: big.length, next_offset: big.length * 2, size: big.length * 2, data: b2 }, 500 * 1024)
  assert.equal(w.headOffset, big.length)
  assert.equal(w.tailOffset, big.length * 2)
  assert.equal(logText(w), b2)
})

test('applyPrepend 向前加载更早日志并拼接为完整内容', () => {
  let s = applyTail({ offset: 20, next_offset: 30, size: 30, data: 'part3-end\n' })
  s = applyPrepend(s, { offset: 10, next_offset: 20, size: 30, data: 'part2-mid\n' })
  s = applyPrepend(s, { offset: 0, next_offset: 10, size: 30, data: 'part1-beg\n' })
  assert.equal(s.headOffset, 0)
  assert.equal(s.tailOffset, 30)
  assert.equal(loadedBytes(s), 30)
  assert.equal(logText(s), 'part1-beg\npart2-mid\npart3-end\n')
})

test('日志被截断或重置时 applyAppend 自动重置缓冲', () => {
  const s = applyTail({ offset: 500, next_offset: 1000, size: 1000, data: 'old' })
  const reset = applyAppend(s, { offset: 0, next_offset: 10, size: 10, data: 'new-start\n' })
  assert.equal(reset.headOffset, 0)
  assert.equal(reset.tailOffset, 10)
  assert.equal(logText(reset), 'new-start\n')
})

test('trimLogState、needsTailJump 与 formatLogBytes 行为正确', () => {
  const s = emptyLogState()
  assert.equal(trimLogState(s, 1024), s)
  assert.equal(needsTailJump(0, 5 * 1024 * 1024), true)
  assert.equal(needsTailJump(2 * 1024 * 1024, 5 * 1024 * 1024), false)
  assert.equal(formatLogBytes(512), '0.5 KB')
  assert.equal(formatLogBytes(1024 * 1024), '1.00 MB')
  assert.equal(formatLogBytes(2.5 * 1024 * 1024), '2.50 MB')
})

test('mockJobs.log 支持 tail 和 end 按 UTF-8 字节倒序拉取且无乱码', () => {
  const full = mockJobs.log(1285, { offset: 0 })
  assert.ok(full.size > 100)
  const tail = mockJobs.log(1285, { tail: 80 })
  assert.equal(tail.next_offset, full.size)
  assert.ok(tail.offset > 0)
  assert.ok(!tail.data.includes('\ufffd'))
  const earlier = mockJobs.log(1285, { tail: full.size, end: tail.offset })
  assert.equal(earlier.offset, 0)
  assert.equal(earlier.next_offset, tail.offset)
  assert.equal(earlier.data + tail.data, full.data)
})
