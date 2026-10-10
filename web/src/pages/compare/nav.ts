// 换图规则（纯函数）：单击只换该结果集、修饰键+单击按序号同步、方向键全部前进后退一张，语义与改版前一致

/** 各结果集的图片数，未加载的为 0 */
export type Lens = Record<number, number>

/** 当前序号夹到 [0, n-1]，没有图片时为 0 */
export const clampIdx = (i: number | undefined, n: number) => Math.max(0, Math.min(i ?? 0, Math.max(0, n - 1)))

/** 全部结果集各自前进 / 后退 d 张，到头停住 */
export function stepAll(cur: Record<number, number>, ids: number[], lens: Lens, d: number): Record<number, number> {
  const next = { ...cur }
  for (const x of ids) next[x] = Math.min(Math.max(0, (lens[x] ?? 0) - 1), Math.max(0, clampIdx(cur[x], lens[x] ?? 0) + d))
  return next
}

/** 按序号同步：全部结果集跳到第 i 张，不够长的停在最后一张 */
export function syncAll(cur: Record<number, number>, ids: number[], lens: Lens, i: number): Record<number, number> {
  const next = { ...cur }
  for (const x of ids) next[x] = Math.min(i, Math.max(0, (lens[x] ?? 0) - 1))
  return next
}

export const atStart = (cur: Record<number, number>, ids: number[], lens: Lens) => ids.every((x) => clampIdx(cur[x], lens[x] ?? 0) === 0)
export const atEnd = (cur: Record<number, number>, ids: number[], lens: Lens) => ids.every((x) => clampIdx(cur[x], lens[x] ?? 0) >= (lens[x] ?? 0) - 1)
