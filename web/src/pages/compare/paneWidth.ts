// 可拖动分栏的宽度计算与保存，不依赖 React，便于单元测试

export interface Bounds { min: number; max: number }

/** 夹到 [min, max]；max 比 min 还小时（窗口很窄）以 min 为准 */
export function clampW(w: number, b: Bounds): number {
  return Math.round(Math.min(Math.max(w, b.min), Math.max(b.min, b.max)))
}

/** 键盘调整：←/→ 每次 16 px，按住 Shift 64 px；Home / End 到最小 / 最大。其他键返回 null */
export function keyWidth(key: string, shift: boolean, w: number, b: Bounds): number | null {
  const step = shift ? 64 : 16
  switch (key) {
    case 'ArrowLeft': return clampW(w - step, b)
    case 'ArrowRight': return clampW(w + step, b)
    case 'Home': return clampW(b.min, b)
    case 'End': return clampW(b.max, b)
    default: return null
  }
}

export interface Store { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem(k: string): void }

const store = (): Store | null => {
  try { return globalThis.localStorage ?? null } catch { return null }
}

/** 读保存的宽度；没有或不合法时返回 null（用默认宽度） */
export function loadW(key: string, s: Store | null = store()): number | null {
  try {
    const raw = s?.getItem(key)
    const v = raw ? Number(raw) : NaN
    return Number.isFinite(v) && v > 0 ? Math.round(v) : null
  } catch { return null }
}

/** 保存宽度；null 表示恢复默认，删除保存的值 */
export function saveW(key: string, w: number | null, s: Store | null = store()): void {
  try {
    if (w === null) s?.removeItem(key)
    else s?.setItem(key, String(Math.round(w)))
  } catch { /* 隐私模式等写不进去时只在本次会话生效 */ }
}
