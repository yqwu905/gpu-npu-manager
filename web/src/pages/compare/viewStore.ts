// 缩放平移的 store，不经过 React：输入事件直接改 store，订阅者（缩放比例文字等）和标脏的格子在同一个 rAF 里更新，
// 缩放、拖动期间没有 React 提交
import { clampView, zoomAt, type View } from './geometry.ts'

export interface Drawable { draw(): void }

export interface ViewStore {
  get(): View
  /** 立即更新视图（已按需夹紧），订阅者与格子在下一帧更新 */
  set(v: View | ((v: View) => View)): void
  /** 滚轮缩放：同一帧内的多次调用累积成一次，以最后一次的光标位置为中心 */
  zoom(factor: number, cx: number, cy: number, w: number, h: number): void
  /** 回到适应窗口 */
  reset(): void
  /** 每帧最多回调一次（视图有变化时） */
  subscribe(fn: (v: View) => void): () => void
  /** 注册格子，返回注销函数；视图变化时全部格子重画 */
  add(cell: Drawable): () => void
  /** 标脏并安排下一帧：不传参数时全部格子 */
  mark(cell?: Drawable): void
  /** 立刻执行挂起的一帧 */
  flush(): void
  dispose(): void
}

export const FIT: View = { z: 1, x: 0, y: 0 }

export function createViewStore(
  init: View = FIT,
  raf: (fn: () => void) => number = (fn) => requestAnimationFrame(fn),
  caf: (id: number) => void = (id) => cancelAnimationFrame(id),
): ViewStore {
  let v = init
  let notified = v
  let frame = 0
  let wheel: { f: number; cx: number; cy: number; w: number; h: number } | null = null
  const subs = new Set<(v: View) => void>()
  const cells = new Set<Drawable>()
  const dirty = new Set<Drawable>()

  const schedule = () => { if (!frame) frame = raf(run) }
  const markAll = () => { for (const c of cells) dirty.add(c) }

  function run() {
    frame = 0
    if (wheel) {
      const p = wheel
      wheel = null
      const n = zoomAt(v, p.f, p.cx, p.cy, p.w, p.h)
      if (n.z !== v.z || n.x !== v.x || n.y !== v.y) { v = n; markAll() }
    }
    if (v !== notified) {
      notified = v
      for (const fn of subs) fn(v)
    }
    const list = [...dirty]
    dirty.clear()
    for (const c of list) c.draw()
  }

  const store: ViewStore = {
    get: () => v,
    set(next) {
      const n = typeof next === 'function' ? next(v) : next
      if (n.z === v.z && n.x === v.x && n.y === v.y) return
      v = n
      markAll()
      schedule()
    },
    zoom(factor, cx, cy, w, h) {
      wheel = wheel ? { f: wheel.f * factor, cx, cy, w, h } : { f: factor, cx, cy, w, h }
      schedule()
    },
    reset() {
      store.set(FIT)
    },
    subscribe(fn) {
      subs.add(fn)
      return () => { subs.delete(fn) }
    },
    add(cell) {
      cells.add(cell)
      dirty.add(cell)
      schedule()
      return () => { cells.delete(cell); dirty.delete(cell) }
    },
    mark(cell) {
      if (cell) dirty.add(cell)
      else markAll()
      schedule()
    },
    flush() {
      if (frame) caf(frame)
      run()
    },
    dispose() {
      if (frame) caf(frame)
      frame = 0
      subs.clear()
      cells.clear()
      dirty.clear()
    },
  }
  return store
}

/** 拖动平移：起点视图加上指针位移，夹紧到图片层仍铺满视口 */
export const panFrom = (start: View, dx: number, dy: number, w: number, h: number) => clampView(start.z, start.x + dx, start.y + dy, w, h)
