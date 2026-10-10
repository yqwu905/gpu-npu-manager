// Alt+单击的文件名匹配（纯函数，Worker 与单测共用）：编辑距离最小者，距离相同取序号小的，与逐个计算的结果完全一致。
// 先用几个“种子”（同序号、同编号、去掉公共前后缀后相同）得到较紧的上界，再全量扫描，超过上界的提前放弃

let rowA = new Int32Array(64), rowB = new Int32Array(64)

/** 编辑距离（按 UTF-16 码元比较）；结果大于 max 时返回 max + 1 */
export function boundedDistance(a: string, b: string, max: number): number {
  if (a.length > b.length) { const t = a; a = b; b = t }
  let n = a.length, m = b.length
  if (m - n > max) return max + 1
  // 去掉公共前缀与后缀，文件名大多只在中间几位不同
  let s = 0
  while (s < n && a.charCodeAt(s) === b.charCodeAt(s)) s++
  while (n > s && a.charCodeAt(n - 1) === b.charCodeAt(m - 1)) { n--; m-- }
  n -= s; m -= s
  if (n === 0) return m <= max ? m : max + 1
  if (max > m) max = m
  const big = max + 1
  if (rowA.length <= m + 1) { rowA = new Int32Array(m + 64); rowB = new Int32Array(m + 64) }
  let prev = rowA, cur = rowB
  for (let j = 0; j <= m; j++) prev[j] = j <= max ? j : big
  // 只算 |i - j| <= max 的斜带；带外的格子视为 big
  for (let i = 1; i <= n; i++) {
    const lo = Math.max(1, i - max), hi = Math.min(m, i + max)
    const ca = a.charCodeAt(s + i - 1)
    let rowMin = cur[lo - 1] = lo === 1 ? Math.min(i, big) : big
    for (let j = lo; j <= hi; j++) {
      let d = prev[j - 1] + (ca === b.charCodeAt(s + j - 1) ? 0 : 1)
      const up = prev[j] + 1, left = cur[j - 1] + 1
      if (up < d) d = up
      if (left < d) d = left
      if (d > big) d = big
      cur[j] = d
      if (d < rowMin) rowMin = d
    }
    if (hi < m) cur[hi + 1] = big
    if (rowMin > max) return big
    const t = prev; prev = cur; cur = t
  }
  return prev[m] > max ? big : prev[m]
}

/** 文件名中最长的一段数字（同长取第一段），用来对齐不同命名方式的同一编号 */
export function serialOf(name: string): string {
  let best = '', i = 0
  while (i < name.length) {
    const c = name.charCodeAt(i)
    if (c >= 48 && c <= 57) {
      let j = i + 1
      while (j < name.length && name.charCodeAt(j) >= 48 && name.charCodeAt(j) <= 57) j++
      if (j - i > best.length) best = name.slice(i, j)
      i = j
    } else i++
  }
  return best
}

export interface MatchSet {
  names: string[]
  /** 全部文件名的公共前缀、后缀长度（只有一个文件时为 0） */
  pre: number
  suf: number
  /** 以下都记第一次出现的序号 */
  byName: Map<string, number>
  bySerial: Map<string, number>
  byCore: Map<string, number>
}

const firstOf = (m: Map<string, number>, k: string, i: number) => { if (!m.has(k)) m.set(k, i) }

export function prepareSet(names: string[]): MatchSet {
  const n = names.length
  let pre = 0, suf = 0
  if (n > 1) {
    let minLen = Infinity
    for (const s of names) if (s.length < minLen) minLen = s.length
    pre = minLen
    const a = names[0]
    for (let k = 1; k < n && pre > 0; k++) {
      const b = names[k]
      let i = 0
      while (i < pre && a.charCodeAt(i) === b.charCodeAt(i)) i++
      pre = i
    }
    suf = minLen - pre
    for (let k = 1; k < n && suf > 0; k++) {
      const b = names[k]
      let i = 0
      while (i < suf && a.charCodeAt(a.length - 1 - i) === b.charCodeAt(b.length - 1 - i)) i++
      suf = i
    }
  }
  const byName = new Map<string, number>(), bySerial = new Map<string, number>(), byCore = new Map<string, number>()
  for (let i = 0; i < n; i++) {
    const s = names[i]
    firstOf(byName, s, i)
    const d = serialOf(s)
    if (d) firstOf(bySerial, d, i)
    if (n > 1) firstOf(byCore, s.slice(pre, s.length - suf), i)
  }
  return { names, pre, suf, byName, bySerial, byCore }
}

/** set 中与 q 编辑距离最小的序号，距离相同取序号小的；set 为空返回 -1。hint 为同序号，src 为 q 所在结果集（用于去公共前后缀） */
export function nearest(q: string, set: MatchSet, hint = -1, src?: MatchSet): number {
  const names = set.names, n = names.length
  if (!n) return -1
  const exact = set.byName.get(q)
  if (exact !== undefined) return exact
  let best = Infinity, bk = -1
  const tryK = (k: number | undefined) => {
    if (k === undefined || k < 0 || k >= n || k === bk) return
    const d = boundedDistance(q, names[k], best === Infinity ? Math.max(q.length, names[k].length) : best)
    if (d < best || (d === best && k < bk)) { best = d; bk = k }
  }
  tryK(hint)
  const serial = serialOf(q)
  if (serial) tryK(set.bySerial.get(serial))
  if (src && src.names.length > 1 && q.length >= src.pre + src.suf) tryK(set.byCore.get(q.slice(src.pre, q.length - src.suf)))
  const ql = q.length
  for (let k = 0; k < n; k++) {
    const b = names[k]
    if (k === bk || Math.abs(b.length - ql) > best) continue
    if (k > bk && Math.abs(b.length - ql) === best && best !== Infinity) continue
    const d = boundedDistance(q, b, best === Infinity ? Math.max(ql, b.length) : best)
    if (d < best || (d === best && k < bk)) { best = d; bk = k }
  }
  return bk
}
