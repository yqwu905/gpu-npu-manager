import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ImageIndex } from './imageIndex.ts'
import type { MatchMsg, MatchReply } from './matchWorker.ts'

// 假 Worker：按顺序异步处理消息，用真正的 matchWorker.ts 逻辑
const g = globalThis as Record<string, unknown>
let onWorker: ((e: { data: MatchMsg }) => void) | null = null
let client: { onmessage: (e: { data: MatchReply }) => void } | null = null
const log: string[] = []
g.self = {
  postMessage: (r: MatchReply) => setTimeout(() => client!.onmessage({ data: r }), 0),
  set onmessage(f: (e: { data: MatchMsg }) => void) { onWorker = f },
}
await import('./matchWorker.ts')
g.Worker = class {
  onmessage = () => {}
  onerror = () => {}
  constructor() { client = this as unknown as typeof client }
  postMessage(m: MatchMsg) {
    log.push(m.op === 'nearest' ? m.op : `${m.op}:${m.id}`)
    setTimeout(() => onWorker!({ data: m }), 0)
  }
  terminate() {}
}
const { matchName, preloadMatch, retainMatcher } = await import('./matchClient.ts')
const { nearest, prepareSet } = await import('./match.ts')

const mk = (id: number) => {
  const names = Array.from({ length: 50 }, (_, k) => `s${id}_img_${String(k).padStart(4, '0')}.png`)
  const byName = new Map<string, number>()
  names.forEach((n, i) => byName.has(n) || byName.set(n, i))
  return { id, etag: `e${id}`, n: names.length, names, byName } as unknown as ImageIndex
}

test('超过 12 个结果集时，一次查询用到的都不丢；预加载重复执行不重传', async () => {
  const sets = Array.from({ length: 14 }, (_, k) => mk(k + 1))
  retainMatcher()  // 不释放：释放后 5 秒才结束 Worker，测试进程要多等 5 秒
  preloadMatch(sets)
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(log.filter((x) => x.startsWith('set')).length, 14)
  assert.ok(!log.some((x) => x.startsWith('drop')))
  log.length = 0
  preloadMatch(sets)
  assert.equal(log.length, 0)
  const [src, ...targets] = sets
  const q = src.names[30]
  const r = matchName(q, src, targets)
  const got = await r.pending
  const ps = prepareSet(src.names)
  assert.deepEqual(got, Object.fromEntries(targets.map((t) => [t.id, nearest(q, prepareSet(t.names), -1, ps)])))
  assert.deepEqual(log, ['nearest'])
  // 少选了两个后，多出的按最久没用的丢掉
  log.length = 0
  preloadMatch(sets.slice(0, 12))
  assert.deepEqual(log.sort(), ['drop:13', 'drop:14'])
})
