// 缩略图栏：所有结果集共用一个虚拟滚动容器，行数取最长的列表，只渲染可见行上下各 4 行，列标题吸顶。
// 快速滚动时行里只有序号和文件名，停下 80 ms 后才请求缩略图（可见行 P3、overscan P5），滚出范围的行释放请求
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { resultsApi } from '../../api/results.ts'
import type { ImageIndex, IndexState } from './imageIndex.ts'
import { PRIO } from './loader.ts'
import { clampIdx } from './nav.ts'
import { perf } from './perf.ts'
import { thumbs, useThumb } from './thumbs.ts'
import { ROW_H, follow, rangeOf, sameRange, vmap, type Range } from './virtual.ts'

export interface Col { id: number; name: string; color: string; st: IndexState }
export interface Mods { ctrlKey: boolean; metaKey: boolean; altKey: boolean }

interface Props {
  cols: Col[]
  cur: Record<number, number>
  focus: number | undefined
  open: boolean
  setOpen: (v: boolean) => void
  modKey: string
  onPick: (id: number, i: number, e: Mods) => void
  onFocus: (id: number) => void
  /** 悬停 80 ms 后预取该图的预览 */
  onHover: (id: number, i: number, e: Mods) => void
}

/** 滚动速度超过 2 px/ms 视为快速滚动；最后一次滚动 80 ms 后恢复加载 */
const FAST = 2
const IDLE_MS = 80
const HOVER_MS = 80

interface CellProps { id: number; ix: ImageIndex; i: number; label: string; color: string; on: boolean; load: boolean; prio: number; modKey: string }

const ThumbCell = memo(function ThumbCell({ id, ix, i, label, color, on, load, prio, modKey }: CellProps) {
  const name = ix.names[i]
  const missing = ix.sizes[i] === -1
  const url = missing ? null : resultsApi.imageUrl(id, ix.paths[i], ix.vs[i], 'thumb')
  // 一旦允许加载就保持到卸载；已缓存的缩略图滚动中也直接显示
  const armed = useRef(false)
  if (load) armed.current = true
  const { src, error } = useThumb(url && (armed.current || thumbs.peek(url) !== null) ? url : null, prio)
  return (
    <button type="button" className="cmp-thumb" data-id={id} data-i={i} aria-current={on} aria-label={`${label} ${name}`}
      title={`${name}\n单击：仅切换该结果\n${modKey}+单击：按序号同步全部\nAlt+单击：按文件名匹配`}
      style={on ? { background: 'var(--bg)', boxShadow: `inset 0 0 0 2px ${color}` } : undefined}>
      {src
        ? <img className="thumb" src={src} alt="" decoding="async" draggable={false} style={{ borderRadius: 4 }} />
        : <span className="thumb cmp-thumb-ph" style={{ borderRadius: 4 }}>{missing ? '文件不存在' : error ?? ''}</span>}
      <span className="row" style={{ gap: 4, width: '100%', minWidth: 0 }}>
        <span className="mono" style={{ fontSize: 10, color: 'var(--faint)', flexShrink: 0 }}>{i + 1}</span>
        <span className="mono ellipsis" style={{ fontSize: 11 }}>{name}</span>
      </span>
    </button>
  )
})

interface RowProps { i: number; top: number; cols: Col[]; sel: string; load: boolean; prio: number; modKey: string }

const ThumbRow = memo(function ThumbRow({ i, top, cols, sel, load, prio, modKey }: RowProps) {
  perf.render('thumbRow')
  return (
    <div className="cmp-vrow" style={{ top }}>
      {cols.map((c, k) => {
        const ix = c.st.ix
        return ix && i < ix.n
          ? <ThumbCell key={c.id} id={c.id} ix={ix} i={i} label={c.name} color={c.color} on={sel[k] === '1'} load={load} prio={prio} modKey={modKey} />
          : <span key={c.id} />
      })}
    </div>
  )
})

function ThumbPanel({ cols, cur, focus, open, setOpen, modKey, onPick, onFocus, onHover }: Props) {
  perf.render('thumbPanel')
  const n = cols.length
  const rows = cols.reduce((m, c) => Math.max(m, c.st.ix?.n ?? 0), 0)
  const sc = useRef<HTMLDivElement>(null)
  const head = useRef<HTMLDivElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const [viewH, setViewH] = useState(0)
  const vm = useMemo(() => vmap(rows, viewH), [rows, viewH])
  const [range, setRange] = useState<Range>(() => rangeOf(0, viewH, rows))
  const [idle, setIdle] = useState(true)
  const live = useRef({ vm, viewH, rows, range, idle })
  live.current = { vm, viewH, rows, range, idle }

  // 超长列表按比例映射滚动位置时，行容器整体平移；行内只用相对 range.start 的位置，数值不会过大
  const place = () => {
    const el = sc.current, b = body.current
    if (!el || !b) return
    const L = live.current
    b.style.transform = L.vm.scaled ? `translateY(${L.range.start * ROW_H + L.vm.offset(el.scrollTop)}px)` : ''
  }

  // 可见高度 = 容器高度 − 吸顶标题
  useLayoutEffect(() => {
    const el = sc.current!, hd = head.current!
    const measure = () => setViewH(Math.max(0, el.clientHeight - hd.offsetHeight))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    ro.observe(hd)
    return () => ro.disconnect()
  }, [])

  useLayoutEffect(() => {
    const el = sc.current!
    const r = rangeOf(vm.toVirtual(el.scrollTop), viewH, rows)
    setRange((old) => (sameRange(old, r) ? old : r))
  }, [vm, viewH, rows])
  useLayoutEffect(place, [range, vm])

  // 滚动：每帧最多算一次范围，范围变了才提交；快速滚动时暂停加载缩略图
  useEffect(() => {
    const el = sc.current!
    let frame = 0, timer: ReturnType<typeof setTimeout> | null = null
    let lastT = 0, lastY = el.scrollTop
    const update = () => {
      frame = 0
      const L = live.current
      const r = rangeOf(L.vm.toVirtual(el.scrollTop), L.viewH, L.rows)
      if (!sameRange(r, L.range)) setRange(r)
      place()
    }
    const onScroll = () => {
      const t = performance.now(), y = el.scrollTop
      if (Math.abs(y - lastY) / Math.max(1, t - lastT) > FAST && live.current.idle) setIdle(false)
      lastT = t
      lastY = y
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = null; setIdle(true) }, IDLE_MS)
      if (!frame) frame = requestAnimationFrame(update)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(frame)
      if (timer) clearTimeout(timer)
    }
  }, [])

  // 对齐基准结果集的当前图不在视口内时直接跳过去（不做平滑滚动）
  const focusCol = cols.find((c) => c.id === focus)
  const fi = focusCol?.st.ix?.n ? clampIdx(cur[focusCol.id], focusCol.st.ix.n) : -1
  useLayoutEffect(() => {
    const el = sc.current!
    if (fi < 0 || !viewH) return
    const t = follow(fi, vm.toVirtual(el.scrollTop), viewH)
    if (t !== null) el.scrollTop = vm.toPhysical(t)
  }, [fi, focus]) // eslint-disable-line react-hooks/exhaustive-deps

  // 事件委托：单击与悬停都挂在滚动容器上
  const pick = (e: ReactMouseEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('[data-i]')
    if (!b) return
    if (e.altKey) e.preventDefault()
    onPick(Number(b.dataset.id), Number(b.dataset.i), e)
  }
  const hov = useRef<{ key: string; t: ReturnType<typeof setTimeout> | null }>({ key: '', t: null })
  const over = (e: ReactPointerEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('[data-i]')
    const key = b ? `${b.dataset.id}:${b.dataset.i}` : ''
    const h = hov.current
    if (key === h.key) return
    if (h.t) clearTimeout(h.t)
    h.key = key
    h.t = null
    if (!b) return
    const id = Number(b.dataset.id), i = Number(b.dataset.i), mods = { ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey }
    h.t = setTimeout(() => { h.t = null; onHover(id, i, mods) }, HOVER_MS)
  }
  const leave = () => {
    const h = hov.current
    if (h.t) clearTimeout(h.t)
    h.key = ''
    h.t = null
  }
  useEffect(() => leave, [])

  const curIdx = cols.map((c) => (c.st.ix?.n ? clampIdx(cur[c.id], c.st.ix.n) : -1))
  // 折叠时面板只是隐藏，跟随当前图滚动出来的新行不请求缩略图，展开后再加载
  const load = idle && open
  const items = []
  for (let i = range.start; i <= range.end; i++) {
    const sel = curIdx.map((k) => (k === i ? '1' : '0')).join('')
    items.push(<ThumbRow key={i} i={i} top={(vm.scaled ? i - range.start : i) * ROW_H} cols={cols} sel={sel} load={load}
      prio={i >= range.first && i <= range.last ? PRIO.thumbs : PRIO.overscan} modKey={modKey} />)
  }
  const thumbW = Math.min(Math.max(n, 1), 4) * 120 + 10
  const gridW = Math.max(n, 1) * 120 + 8

  return (
    <div className="cmp-fold" style={{ flexBasis: open ? thumbW : 44 }}>
      <section aria-label="缩略图" aria-hidden={!open} className={open ? 'card cmp-panel' : 'card cmp-panel off'} style={{ width: thumbW }}>
        <div className="cmp-panel-head">
          <h2 className="grow" style={{ fontSize: 15 }}>缩略图</h2>
          <button type="button" className="cmp-icon-btn" onClick={() => setOpen(false)} aria-label="折叠缩略图栏" aria-expanded="true">‹</button>
        </div>
        <div ref={sc} className="cmp-vlist" onClick={pick} onPointerOver={over} onPointerLeave={leave}>
          <div ref={head} className="cmp-vhead" style={{ gridTemplateColumns: `repeat(${Math.max(n, 1)}, 112px)`, width: gridW }}>
            {cols.map((c, k) => {
              const ix = c.st.ix, len = ix?.n ?? 0
              const note = ix?.missing ? `${ix.missing} 个文件不存在` : undefined
              return (
                <div key={c.id} className="col" style={{ gap: 4, minWidth: 0 }}>
                  <button type="button" className="cmp-col-head" onClick={() => onFocus(c.id)} aria-label={`以 ${c.name} 为对齐基准`} title={note} style={{ borderBottomColor: c.color }}>
                    <span className="row" style={{ gap: 6, minWidth: 0, width: '100%' }}>
                      <span className="dot" style={{ width: 9, height: 9, background: c.color }} />
                      <span className="mono ellipsis" style={{ fontSize: 12, fontWeight: c.id === focus ? 600 : 400 }}>{c.name}</span>
                    </span>
                    <span className="mono lbl" style={{ fontSize: 11 }}>{len ? `${curIdx[k] + 1} / ${len}` : c.st.loading ? '加载中' : '0 张'}</span>
                  </button>
                  {c.st.error && <span className="lbl" style={{ color: '#A1281F' }}>{c.st.error}</span>}
                  {ix?.truncated && <span className="lbl" style={{ color: '#5C3F00' }}>只列出前 {len} 张</span>}
                </div>
              )
            })}
          </div>
          <div className="cmp-vspace" style={{ height: vm.height, width: gridW }}>
            <div ref={body} className="cmp-vbody" style={{ ['--cols' as string]: Math.max(n, 1) }}>{items}</div>
          </div>
        </div>
      </section>
      <button type="button" className={open ? 'cmp-rail off' : 'cmp-rail'} onClick={() => setOpen(true)} aria-label="展开缩略图栏" aria-expanded="false">
        <span aria-hidden="true">›</span>
        <span className="cmp-rail-title">缩略图</span>
      </button>
    </div>
  )
}

export default memo(ThumbPanel)
