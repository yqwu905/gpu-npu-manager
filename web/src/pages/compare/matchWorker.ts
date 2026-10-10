// Alt+单击文件名匹配的 Worker：各结果集的文件名只传一次，查询时按编辑距离找最接近的序号，不占主线程
import { nearest, prepareSet, type MatchSet } from './match.ts'

export type MatchMsg =
  | { op: 'set'; id: number; names: string[] }
  | { op: 'drop'; id: number }
  | { op: 'nearest'; seq: number; q: string; src: number; ids: number[]; hints: Record<number, number> }
export interface MatchReply { seq: number; result: Record<number, number> }

const sets = new Map<number, MatchSet>()

self.onmessage = (e: MessageEvent<MatchMsg>) => {
  const m = e.data
  if (m.op === 'set') sets.set(m.id, prepareSet(m.names))
  else if (m.op === 'drop') sets.delete(m.id)
  else {
    const result: Record<number, number> = {}
    for (const id of m.ids) {
      const s = sets.get(id)
      if (s) result[id] = nearest(m.q, s, m.hints[id] ?? -1, sets.get(m.src))
    }
    const reply: MatchReply = { seq: m.seq, result }
    self.postMessage(reply)
  }
}
