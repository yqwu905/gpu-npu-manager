// 解码后图片的 LRU：按字节预算淘汰最久未用的项并 close()；pin 住的项（正在绘制、有句柄引用）不淘汰
import type { Entry } from './loader.ts'

export interface LRU<T> {
  /** 取出并标为最近使用 */
  get(key: string): T | undefined
  /** 只读不改顺序，绘制时用 */
  peek(key: string): T | undefined
  has(key: string): boolean
  /** 放入（同 key 的旧值会被释放），随后按预算淘汰 */
  set(key: string, value: T, bytes: number): void
  delete(key: string): void
  /** 引用计数；可以先于 set 调用，放入后即受保护 */
  pin(key: string): void
  unpin(key: string): void
  isPinned(key: string): boolean
  /** 释放全部项并清空引用计数 */
  clear(): void
  /** 当前总字节数（含 pin 住的） */
  readonly used: number
  readonly size: number
  pinnedBytes(): number
  budget: number
  /** 按预算淘汰一次（预算调小后调用） */
  trim(): void
}

export function createLRU<T>(budget: number, dispose: (value: T, key: string) => void): LRU<T> {
  // Map 保持插入顺序：最前面是最久未用的
  const items = new Map<string, { value: T; bytes: number }>()
  const pins = new Map<string, number>()
  let used = 0

  const drop = (key: string) => {
    const it = items.get(key)
    if (!it) return
    items.delete(key)
    used -= it.bytes
    dispose(it.value, key)
  }
  const trim = (keep?: string) => {
    if (used <= lru.budget) return
    // 遍历中删除 Map 的项是安全的
    for (const key of items.keys()) {
      if (used <= lru.budget) break
      if (key !== keep && !pins.has(key)) drop(key)
    }
  }

  const lru: LRU<T> = {
    budget,
    get(key) {
      const it = items.get(key)
      if (!it) return undefined
      items.delete(key)
      items.set(key, it)
      return it.value
    },
    peek: (key) => items.get(key)?.value,
    has: (key) => items.has(key),
    set(key, value, bytes) {
      const old = items.get(key)
      if (old) {
        items.delete(key)
        used -= old.bytes
        if (old.value !== value) dispose(old.value, key)
      }
      items.set(key, { value, bytes })
      used += bytes
      trim(key)
    },
    delete: drop,
    pin(key) {
      pins.set(key, (pins.get(key) ?? 0) + 1)
    },
    unpin(key) {
      const n = pins.get(key)
      if (n === undefined) return
      if (n > 1) pins.set(key, n - 1)
      else {
        pins.delete(key)
        trim()
      }
    },
    isPinned: (key) => pins.has(key),
    clear() {
      for (const key of items.keys()) drop(key)
      pins.clear()
    },
    get used() {
      return used
    },
    get size() {
      return items.size
    },
    pinnedBytes() {
      let n = 0
      for (const [key, it] of items) if (pins.has(key)) n += it.bytes
      return n
    },
    trim: () => trim(),
  }
  return lru
}

const MB = 1024 * 1024
/** 解码图片的内存预算：设备内存 GB × 128 MB，限制在 384–1024 MB（不含 pin 住的） */
export const BUDGET = Math.min(1024, Math.max(384, ((typeof navigator !== 'undefined' && (navigator as Navigator & { deviceMemory?: number }).deviceMemory) || 4) * 128)) * MB

/** 释放解码结果：ImageBitmap 调 close()，<img> 回退时撤销 objectURL（与缩略图栏共用的 objectURL 归缩略图缓存，不撤销） */
export function closeSource(e: Entry) {
  const b = e.bitmap
  if (!b) return
  if ('close' in b) b.close()
  else if (b.src.startsWith('blob:') && !b.dataset.shared) URL.revokeObjectURL(b.src)
}

/** 全页共用的解码缓存，key 为图片 URL */
export const bitmaps: LRU<Entry> = createLRU<Entry>(BUDGET, closeSource)
