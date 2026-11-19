// 可拖动分栏：拖动条放在栏和右侧内容之间的间距里。拖动期间直接改 DOM 宽度（每帧最多一次），
// 不经过 React，松开后才提交并保存；双击恢复默认宽度，聚焦后可用 ←/→（Shift 加大步长）、Home / End 调整
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { clampW, keyWidth, loadW, saveW, type Bounds } from './paneWidth.ts'

/** 栏最多占窗口宽度的比例，保证主区域始终留有空间 */
const MAX_FRAC = 0.45

interface Opts { storageKey: string; def: number; min: number; max: number }

/** 栏宽：拖动过的宽度保存在 localStorage，没有时用默认宽度；最大宽度同时受窗口宽度限制 */
export function usePaneWidth({ storageKey, def, min, max }: Opts) {
  const [user, setUser] = useState<number | null>(() => loadW(storageKey))
  const [winW, setWinW] = useState(() => window.innerWidth)
  useEffect(() => {
    let frame = 0
    const on = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; setWinW(window.innerWidth) }) }
    window.addEventListener('resize', on)
    return () => { window.removeEventListener('resize', on); cancelAnimationFrame(frame) }
  }, [])
  const bounds: Bounds = { min, max: Math.min(max, Math.round(winW * MAX_FRAC)) }
  const width = clampW(user ?? def, bounds)
  const set = (w: number | null) => {
    const v = w === null ? null : clampW(w, bounds)
    setUser(v)
    saveW(storageKey, v)
  }
  return { width, bounds, set }
}

interface Props {
  label: string
  width: number
  bounds: Bounds
  /** null 表示恢复默认宽度 */
  onChange: (w: number | null) => void
}

interface Drag { x: number; w: number; cur: number; frame: number; fold: HTMLElement; panel: HTMLElement | null; el: HTMLElement }

/** 放在 .cmp-fold 后面；拖动时改的是前一个兄弟元素（栏）和其中 .cmp-panel 的宽度 */
export function PaneResizer({ label, width, bounds, onChange }: Props) {
  const drag = useRef<Drag | null>(null)
  const apply = (d: Drag) => {
    d.fold.style.flexBasis = `${d.cur}px`
    if (d.panel) d.panel.style.width = `${d.cur}px`
  }
  const end = () => {
    const d = drag.current
    if (!d) return
    drag.current = null
    cancelAnimationFrame(d.frame)
    apply(d)
    delete d.fold.dataset.drag
    delete d.el.dataset.active
    document.documentElement.classList.remove('col-resizing')
    if (d.cur !== d.w) onChange(d.cur)
  }
  useEffect(() => () => document.documentElement.classList.remove('col-resizing'), [])

  const down = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    const el = e.currentTarget, fold = el.previousElementSibling as HTMLElement | null
    if (!fold) return
    e.preventDefault()
    el.setPointerCapture(e.pointerId)
    drag.current = { x: e.clientX, w: width, cur: width, frame: 0, fold, panel: fold.querySelector<HTMLElement>('.cmp-panel'), el }
    // 拖动期间关掉栏的宽度过渡，跟手
    fold.dataset.drag = ''
    el.dataset.active = ''
    document.documentElement.classList.add('col-resizing')
  }
  const move = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d) return
    d.cur = clampW(d.w + e.clientX - d.x, bounds)
    if (!d.frame) d.frame = requestAnimationFrame(() => { d.frame = 0; apply(d) })
  }
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const w = keyWidth(e.key, e.shiftKey, width, bounds)
    if (w === null) return
    e.preventDefault()
    if (w !== width) onChange(w)
  }

  return (
    <div role="separator" aria-orientation="vertical" aria-label={label} aria-valuenow={width} aria-valuemin={bounds.min} aria-valuemax={Math.max(bounds.min, bounds.max)}
      tabIndex={0} className="cmp-resizer" title="拖动调整宽度，双击恢复默认"
      onPointerDown={down} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onLostPointerCapture={end}
      onDoubleClick={() => onChange(null)} onKeyDown={key} />
  )
}
