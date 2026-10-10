// 对比格子：React 只负责标题栏与按钮，画布、文件名、角标和“正在显示”都由 CellRenderer 直接改写，换图、缩放、按住对比都不重渲染
import { memo, useEffect, useLayoutEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createCellRenderer, type CellRenderer } from './CellRenderer.ts'
import type { ImageIndex } from './imageIndex.ts'
import { perf } from './perf.ts'
import type { ViewStore } from './viewStore.ts'

export interface Peer { id: number; name: string; color: string }

interface Props {
  id: number
  name: string
  color: string
  focus: boolean
  mismatch: boolean
  /** 标题栏右侧：“3 / 100”、“加载中”或“无图片” */
  count: string
  ix: ImageIndex | null
  i: number
  /** 其他结果集，每个一个按住切换按钮 */
  peers: Peer[]
  store: ViewStore
  /** 全部格子的渲染器，按住对比时从这里取对方的 */
  cells: Map<number, CellRenderer>
  nameOf: (id: number) => string
  onFocus: (id: number) => void
  /** 预取要重算时回调：预览档位变了，或当前图加载完毕 */
  onPrefetch?: () => void
}

function ImageCell({ id, name, color, focus, mismatch, count, ix, i, peers, store, cells, nameOf, onFocus, onPrefetch }: Props) {
  perf.render('cell')
  const vp = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const fileEl = useRef<HTMLSpanElement>(null)
  const tier = useRef<HTMLSpanElement>(null)
  const flag = useRef<HTMLSpanElement>(null)
  const ph = useRef<HTMLDivElement>(null)
  const holding = useRef<HTMLDivElement>(null)
  const holdDot = useRef<HTMLSpanElement>(null)
  const holdName = useRef<HTMLSpanElement>(null)
  const btns = useRef<HTMLDivElement>(null)
  const r = useRef<CellRenderer | null>(null)
  const names = useRef(nameOf)
  names.current = nameOf
  const prefetchCb = useRef(onPrefetch)
  prefetchCb.current = onPrefetch
  const holdOf = useRef<number | null>(null)

  useLayoutEffect(() => {
    const rr = createCellRenderer({ vp: vp.current!, canvas: canvas.current!, name: fileEl.current!, tier: tier.current!, flag: flag.current!, ph: ph.current! }, store,
      (x) => names.current(x), { onPrefetch: () => prefetchCb.current?.() })
    r.current = rr
    cells.set(id, rr)
    return () => {
      rr.dispose()
      if (cells.get(id) === rr) cells.delete(id)
      if (r.current === rr) r.current = null
    }
  }, [store, cells, id])

  useLayoutEffect(() => { r.current?.setImage(ix ? { id, ix, i } : null) }, [id, ix, i])
  useLayoutEffect(() => { r.current?.relabel() }, [nameOf])

  // 按住对比：直接改 DOM 与渲染器，不经过 React
  const holdOn = (o: Peer, btn: HTMLElement) => {
    const other = cells.get(o.id)
    if (holdOf.current !== null || !other || !r.current) return
    holdOf.current = o.id
    r.current.setHold(other)
    btn.classList.add('on')
    holdDot.current!.style.background = o.color
    holdName.current!.textContent = o.name
    holding.current!.hidden = false
  }
  const holdOff = () => {
    if (holdOf.current === null) return
    holdOf.current = null
    r.current?.setHold(null)
    btns.current?.querySelectorAll('.cmp-hold.on').forEach((b) => b.classList.remove('on'))
    if (holding.current) holding.current.hidden = true
  }
  const holdKey = (o: Peer) => (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); holdOn(o, e.currentTarget) }
  }
  // 结果集增减时结束按住
  useEffect(() => () => holdOff(), [peers]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    // data-pic 为当前图；画布上的 data-pic 是实际画出的图，两者不同时画面还是上一张（最多 100 ms）或按住对比中
    <figure className="card cmp-cell" data-id={id} data-pic={ix?.n ? `${id}:${i}` : undefined} style={{ boxShadow: focus ? `0 0 0 2px ${color}` : undefined }}>
      <figcaption className="row" style={{ gap: 8, padding: '8px 10px', borderTop: `3px solid ${color}`, borderBottom: '1px solid var(--border-soft)' }}>
        <button type="button" className="cmp-name-btn" onClick={() => onFocus(id)} aria-label={`以 ${name} 为对齐基准`}>
          <span className="dot" style={{ width: 10, height: 10, background: color }} />
          <span className="mono ellipsis" style={{ fontSize: 13, fontWeight: 500 }}>{name}</span>
        </button>
        <span className="grow" />
        {mismatch && <span className="badge" style={{ background: '#FFF4D6', color: '#5C3F00' }}>编号不一致</span>}
        <span className="mono lbl">{count}</span>
      </figcaption>
      <div ref={vp} data-viewport="1" className="cmp-vp">
        <canvas ref={canvas} className="cmp-canvas" role="img" />
        <div ref={ph} className="cmp-ph mono" hidden />
        <div className="cmp-badges">
          <span ref={tier} className="badge cmp-tier" hidden />
          <span ref={flag} className="badge cmp-tier" hidden>已归一化</span>
        </div>
        <div ref={holding} className="cmp-holding" hidden>
          <span ref={holdDot} className="dot" />
          正在显示 <span ref={holdName} />
        </div>
        <div ref={btns} className="cmp-hold-btns">
          {peers.map((o) => (
            <button key={o.id} type="button" className="cmp-hold"
              onPointerDown={(e) => holdOn(o, e.currentTarget)} onPointerUp={holdOff} onPointerLeave={holdOff}
              onKeyDown={holdKey(o)} onKeyUp={holdOff} onBlur={holdOff}
              aria-label={`按住查看 ${o.name} 的当前图`}>
              <span className="dot" style={{ width: 10, height: 10, background: o.color, boxShadow: '0 0 0 1.5px #FFFFFF' }} />
              <span className="ellipsis" style={{ maxWidth: 120 }}>{o.name}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="row" style={{ gap: 8, padding: '8px 10px' }}>
        <span ref={fileEl} className="mono ellipsis grow" style={{ fontSize: 12 }} />
        {peers.length > 0 && <span className="lbl" style={{ whiteSpace: 'nowrap' }}>按住右下角按钮切换</span>}
      </div>
    </figure>
  )
}

export default memo(ImageCell)
