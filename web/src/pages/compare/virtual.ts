// 缩略图栏的虚拟滚动（纯函数）：固定行高，只渲染可见行加上下各 overscan 行。
// 行数 × 行高超过 MAX_SCROLL_PX 时（Firefox 元素高度上限约 1790 万像素）按比例把真实 scrollTop 映射到虚拟坐标

export const ROW_H = 112
export const OVERSCAN = 4
export const MAX_SCROLL_PX = 15e6

/** first..last 与视口相交，start..end 再各加 overscan 行；没有行时 last、end 为 -1 */
export interface Range { first: number; last: number; start: number; end: number }

/** top 为虚拟坐标下的滚动位置（未缩放时就是 scrollTop） */
export function rangeOf(top: number, viewH: number, n: number, rowH = ROW_H, overscan = OVERSCAN): Range {
  if (n <= 0) return { first: 0, last: -1, start: 0, end: -1 }
  const first = Math.min(n - 1, Math.max(0, Math.floor(top / rowH)))
  const last = Math.min(n - 1, Math.max(first, Math.ceil((top + Math.max(0, viewH)) / rowH) - 1))
  return { first, last, start: Math.max(0, first - overscan), end: Math.min(n - 1, last + overscan) }
}

export const sameRange = (a: Range, b: Range) => a.first === b.first && a.last === b.last && a.start === b.start && a.end === b.end

/** 让第 i 行完整可见要滚到的虚拟位置；已完整可见时为 null。视口比一行还矮时让行顶对齐 */
export function follow(i: number, top: number, viewH: number, rowH = ROW_H): number | null {
  const y = i * rowH
  if (y >= top && y + rowH <= top + viewH) return null
  return y < top || viewH < rowH ? y : y + rowH - viewH
}

export interface VMap {
  /** 虚拟总高 rows × rowH */
  total: number
  /** 占位元素的真实高度，不超过上限 */
  height: number
  /** 是否在缩放映射 */
  scaled: boolean
  /** 真实 scrollTop → 虚拟滚动位置 */
  toVirtual(scrollTop: number): number
  /** 虚拟滚动位置 → 真实 scrollTop（定位到某行时用） */
  toPhysical(top: number): number
  /** 第 i 行在占位元素里的 top：i × rowH + offset(scrollTop)；未缩放时 offset 恒为 0 */
  offset(scrollTop: number): number
}

/** 两端对齐的线性映射：真实滚到底时虚拟也正好到底 */
export function vmap(n: number, viewH: number, rowH = ROW_H, cap = MAX_SCROLL_PX): VMap {
  const total = Math.max(0, n) * rowH
  if (total <= cap || cap <= viewH) {
    return { total, height: total, scaled: false, toVirtual: (p) => p, toPhysical: (t) => t, offset: () => 0 }
  }
  const ratio = (total - viewH) / (cap - viewH)
  return {
    total, height: cap, scaled: true,
    toVirtual: (p) => p * ratio,
    toPhysical: (t) => t / ratio,
    offset: (p) => p - p * ratio,
  }
}
