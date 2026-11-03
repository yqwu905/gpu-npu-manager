import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { resultsApi } from '../api/results'
import { bitmaps } from './compare/bitmapCache.ts'
import type { CellRenderer } from './compare/CellRenderer.ts'
import type { View } from './compare/geometry.ts'
import ImageCell, { type Peer } from './compare/ImageCell'
import { remapIndex, useImageIndexes, type ImageIndex, type IndexState } from './compare/imageIndex.ts'
import { PRIO, loader } from './compare/loader.ts'
import { serialOf } from './compare/match.ts'
import { matchName, preloadMatch, retainMatcher } from './compare/matchClient.ts'
import { atEnd, atStart, clampIdx, stepAll, syncAll, type Lens } from './compare/nav.ts'
import { installPerf, perf } from './compare/perf.ts'
import { createHoverQueue, createPrefetcher, planPrefetch } from './compare/prefetch.ts'
import ThumbPanel, { type Col, type Mods } from './compare/ThumbPanel'
import { createViewStore, panFrom } from './compare/viewStore.ts'

// Mac 上 Ctrl+左键会被系统当成右键，修饰键改用 ⌘；其他平台用 Ctrl
const IS_MAC = /mac/i.test((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform || navigator.platform || '')
export const MOD_KEY = IS_MAC ? '⌘' : 'Ctrl'
const isMod = (e: { ctrlKey: boolean; metaKey: boolean }) => (IS_MAC ? e.metaKey : e.ctrlKey)
const LOADING: IndexState = { ix: null, loading: true, error: null }

interface Props {
  ids: number[]
  nameOf: (id: number) => string
  colorOf: (id: number) => string
}

/**
 * 图片对比：只做编排。图片列表在 imageIndex，缩略图栏虚拟滚动，格子由 CellRenderer 画在 canvas 上；
 * 缩放平移走 viewStore，不触发 React 提交；当前位置 cur、对齐基准 focus、状态文字与折叠状态放在 React 里
 */
function CompareImages({ ids, nameOf, colorOf }: Props) {
  perf.render('compareImages')
  const [aliases, setAliases] = useState<Record<number, string>>({})
  const displayName = useCallback((id: number) => aliases[id] || nameOf(id), [aliases, nameOf])
  const [copying, setCopying] = useState(false)
  const [cur, setCur] = useState<Record<number, number>>({})
  const [focusPick, setFocus] = useState<number | null>(null)
  const [status, setStatus] = useState('')
  const [thumbsOpen, setThumbsOpen] = useState(true)
  const [store] = useState(() => createViewStore())
  const [cells] = useState(() => new Map<number, CellRenderer>())
  const [pf] = useState(() => ({ main: createPrefetcher(), hover: createHoverQueue() }))
  const dir = useRef<1 | -1>(1)
  const altSeq = useRef(0)
  const gridRef = useRef<HTMLDivElement | null>(null)
  const zoomRef = useRef<HTMLSpanElement | null>(null)

  // 列表在后台重新验证后变了：按路径换算当前位置
  const states = useImageIndexes(ids, (id, prev, next) =>
    setCur((c) => (c[id] === undefined ? c : { ...c, [id]: remapIndex(prev, next, clampIdx(c[id], prev.n)) })))
  const lens: Lens = useMemo(() => Object.fromEntries(ids.map((id) => [id, states[id]?.ix?.n ?? 0])), [ids, states])
  const focus = focusPick !== null && ids.includes(focusPick) ? focusPick : ids[0]
  const live = useRef({ ids, states, lens, cur, nameOf })
  live.current = { ids, states, lens, cur, nameOf }

  useEffect(() => installPerf(), [])
  useEffect(() => retainMatcher(), [])
  // 列表一到就把文件名交给 Worker，第一次 Alt+单击不必再传
  useEffect(() => preloadMatch(ids.flatMap((id) => states[id]?.ix ?? [])), [ids, states])

  const geom = () => cells.values().next().value?.geom() ?? null

  /** 预取：各结果集 ±1、+2 的预览与 +1 的缩略图，放大时再加 +1 同一视口的无损层 */
  const refreshPrefetch = useCallback(() => {
    const { ids, states, cur } = live.current
    const g = geom()
    if (!g) { pf.main.clear(); return }
    const sets = ids.flatMap((id) => { const ix = states[id]?.ix; return ix?.n ? [{ ix, i: clampIdx(cur[id], ix.n) }] : [] })
    const v = store.forSize(g.cw, g.ch)
    // 放大时下一张的无损层等各格当前图都加载完再取，免得和眼前的视图抢服务端解码（格子加载完会回调这里）
    const zoom = v.z > 1 && [...cells.values()].every((c) => c.settled()) ? { v, cw: g.cw, ch: g.ch, dpr: g.dpr } : undefined
    // 只有 pin 住的位图淘汰不掉；没 pin 的缓存放得下新的预取，满了就按 LRU 淘汰
    pf.main.update(planPrefetch({ sets, dir: dir.current, size: g.pv, budget: bitmaps.budget, used: bitmaps.pinnedBytes(), zoom }))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { refreshPrefetch() }, [cur, states, ids, refreshPrefetch])
  useEffect(() => () => { pf.main.clear(); pf.hover.clear() }, [pf])
  // 取消了全部结果集：没有格子在画，释放预取与悬停句柄，中止剩下的请求（解码中的结果也不再进缓存），解码结果全部释放
  useEffect(() => {
    if (ids.length) return
    pf.main.clear()
    pf.hover.clear()
    loader.abortAll()
    const b = bitmaps.budget
    bitmaps.budget = 0
    bitmaps.trim()
    bitmaps.budget = b
  }, [ids.length, pf])

  const pick = useCallback((id: number, i: number, e: Mods) => {
    const { ids, states, lens, nameOf } = live.current
    const ix = states[id]?.ix
    if (!ix || i >= ix.n) return
    const name = ix.names[i]
    const seq = ++altSeq.current
    perf.mark('cmp:switch')
    // 换图后回到适应窗口，不沿用上一张的缩放平移
    store.reset()
    if (isMod(e)) {
      setCur((c) => syncAll(c, ids, lens, i))
      setStatus(`${MOD_KEY} 按序号同步 · 第 ${i + 1} 张`)
    } else if (e.altKey) {
      // 文件名完全相同的当帧应用，其余交给 Worker 按编辑距离找最接近的；期间又有新操作时丢弃结果
      const targets = ids.filter((x) => x !== id).map((x) => states[x]?.ix).filter((t): t is ImageIndex => !!t)
      const r = matchName(name, ix, targets, Object.fromEntries(targets.map((t) => [t.id, Math.min(i, t.n - 1)])))
      const next: Record<number, number> = { [id]: i, ...r.exact }
      for (const x of ids) if (x !== id && !states[x]?.ix?.n) next[x] = 0
      setCur((c) => ({ ...c, ...next }))
      if (r.pending) {
        setStatus(`Alt 文件名匹配中… · ${name}`)
        r.pending.then((res) => {
          if (!res || seq !== altSeq.current) return
          setCur((c) => ({ ...c, ...res }))
          setStatus(`Alt 文件名匹配 · ${name}`)
        })
      } else setStatus(`Alt 文件名匹配 · ${name}`)
    } else {
      setCur((c) => ({ ...c, [id]: i }))
      setStatus(`仅切换 ${nameOf(id)} · ${name}`)
    }
    setFocus(id)
  }, [])

  const step = useCallback((d: 1 | -1) => {
    const { ids, lens } = live.current
    altSeq.current++
    dir.current = d
    loader.noteStep()
    perf.mark('cmp:switch')
    store.reset()
    setCur((c) => stepAll(c, ids, lens, d))
    setStatus(`${d < 0 ? '上一张' : '下一张'} · 全部结果`)
  }, [])

  // 悬停预取：修饰键按住时取全部结果集的同一序号
  const hover = useCallback((id: number, i: number, e: Mods) => {
    const { ids, states } = live.current
    const g = geom()
    if (!g) return
    for (const x of isMod(e) ? ids : [id]) {
      const ix = states[x]?.ix
      const k = ix ? Math.min(i, ix.n - 1) : -1
      if (ix && k >= 0 && ix.sizes[k] !== -1) pf.hover.add(resultsApi.imageUrl(x, ix.paths[k], ix.vs[k], 'preview', { size: g.pv }), PRIO.next)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ←/↑ 上一张，→/↓ 下一张；按着修饰键或在输入框里时不处理
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); step(-1) }
      else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); step(1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [step])

  // 缩放平移挂原生监听：React 的 onWheel 是 passive 的，拦不住浏览器自带的 Ctrl+滚轮页面缩放；改 store 不经过 React
  useEffect(() => {
    const el = gridRef.current
    if (!el) return
    const vpOf = (t: EventTarget | null) => (t as HTMLElement | null)?.closest?.<HTMLElement>('[data-viewport]') ?? null
    const onWheel = (e: WheelEvent) => {
      const vp = vpOf(e.target)
      if (!vp) return
      e.preventDefault()
      const r = vp.getBoundingClientRect()
      store.zoom(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top, r.width, r.height)
    }
    let drag: { id: number; x: number; y: number; v: View; w: number; h: number } | null = null
    const onDown = (e: PointerEvent) => {
      const vp = vpOf(e.target)
      if (!vp || e.button !== 0 || (e.target as HTMLElement).closest('button')) return
      e.preventDefault()
      vp.setPointerCapture(e.pointerId)
      const r = vp.getBoundingClientRect()
      drag = { id: e.pointerId, x: e.clientX, y: e.clientY, v: store.forSize(r.width, r.height), w: r.width, h: r.height }
      el.dataset.drag = '1'
    }
    const onMove = (e: PointerEvent) => {
      if (drag && e.pointerId === drag.id) store.setForSize(panFrom(drag.v, e.clientX - drag.x, e.clientY - drag.y, drag.w, drag.h), drag.w, drag.h)
    }
    const onUp = (e: PointerEvent) => {
      if (!drag || e.pointerId !== drag.id) return
      drag = null
      delete el.dataset.drag
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('pointerdown', onDown)
    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerup', onUp)
    el.addEventListener('pointercancel', onUp)
    el.addEventListener('lostpointercapture', onUp)
    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('pointerdown', onDown)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      el.removeEventListener('pointercancel', onUp)
      el.removeEventListener('lostpointercapture', onUp)
    }
  }, [store])

  // 缩放比例文字与光标样式随 store 每帧更新；放大后停下 150 ms 再刷新无损层预取
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | null = null
    const off = store.subscribe((v) => {
      if (zoomRef.current) zoomRef.current.textContent = `${Math.round(v.z * 100)}%`
      const g = gridRef.current
      if (g) { if (v.z > 1) g.dataset.zoomed = '1'; else delete g.dataset.zoomed }
      if (t) clearTimeout(t)
      t = setTimeout(refreshPrefetch, 150)
    })
    return () => { off(); if (t) clearTimeout(t) }
  }, [store, refreshPrefetch])
  useEffect(() => () => store.dispose(), [store])

  const zoomBy = (factor: number) => {
    const r = gridRef.current?.querySelector('[data-viewport]')?.getBoundingClientRect()
    if (!r) return
    store.zoom(factor, r.width / 2, r.height / 2, r.width, r.height)
  }

  const copyGrid = async () => {
    const grid = gridRef.current
    if (!grid) return
    setCopying(true)
    try {
      if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('当前浏览器不支持复制图片，请使用 HTTPS 或 localhost')
      // 在点击事件内开始剪贴板写入，兼容需要用户手势的浏览器。
      const png = (async () => {
        store.flush()
        const deadline = performance.now() + 15000
        while (true) {
          const ready = [...cells.values()].map((cell) => cell.capture()).every(Boolean)
          if (ready) break
          if (performance.now() > deadline) throw new Error('图片仍在加载，请稍后重试')
          await new Promise((resolve) => setTimeout(resolve, 50))
        }
        const figures = [...grid.querySelectorAll<HTMLElement>('.cmp-cell')]
        const rects = figures.map((f) => f.getBoundingClientRect())
        const left = Math.min(...rects.map((r) => r.left)), top = Math.min(...rects.map((r) => r.top))
        const scale = window.devicePixelRatio || 1
        const out = document.createElement('canvas')
        out.width = Math.ceil((Math.max(...rects.map((r) => r.right)) - left) * scale)
        out.height = Math.ceil((Math.max(...rects.map((r) => r.bottom)) - top) * scale)
        const ctx = out.getContext('2d')!
        ctx.scale(scale, scale)
        ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, out.width / scale, out.height / scale)
        figures.forEach((f, k) => {
          const r = rects[k], canvas = f.querySelector('canvas')!, vp = canvas.getBoundingClientRect()
          const id = Number(f.dataset.id)
          ctx.fillStyle = colorOf(id); ctx.fillRect(r.left - left, r.top - top, r.width, 3)
          ctx.save(); ctx.beginPath(); ctx.rect(r.left - left + 10, r.top - top + 3, r.width - 20, vp.top - r.top - 3); ctx.clip()
          ctx.fillStyle = '#15181c'; ctx.font = '500 13px sans-serif'
          ctx.fillText(displayName(id), r.left - left + 10, r.top - top + 25)
          ctx.restore()
          ctx.fillStyle = '#e6e8e3'; ctx.fillRect(vp.left - left, vp.top - top, vp.width, vp.height)
          ctx.drawImage(canvas, vp.left - left, vp.top - top, vp.width, vp.height)
          ctx.save(); ctx.beginPath(); ctx.rect(r.left - left, vp.bottom - top, r.width, r.bottom - vp.bottom); ctx.clip()
          ctx.fillStyle = '#555'; ctx.font = '12px sans-serif'
          ctx.fillText(f.querySelector('.mono.ellipsis.grow')?.textContent || '', r.left - left + 10, vp.bottom - top + 21)
          ctx.restore()
        })
        return new Promise<Blob>((resolve, reject) => {
          out.toBlob((blob) => blob ? resolve(blob) : reject(new Error('生成图片失败')), 'image/png')
        })
      })()
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])
      setStatus('对比图已复制')
    } catch (e) { setStatus(`复制失败：${e instanceof Error ? e.message : String(e)}`) }
    finally { setCopying(false) }
  }

  const n = ids.length
  const gridCols = n <= 1 ? 1 : n <= 4 ? 2 : 3
  const cols: Col[] = useMemo(() => ids.map((id) => ({ id, name: nameOf(id), color: colorOf(id), st: states[id] ?? LOADING })), [ids, nameOf, colorOf, states])
  const peers = useMemo(() => new Map(ids.map((id) => [id, ids.filter((o) => o !== id).map((o): Peer => ({ id: o, name: nameOf(o), color: colorOf(o) }))])), [ids, nameOf, colorOf])
  const focusIx = states[focus]?.ix
  const focusSerial = focusIx?.n ? serialOf(focusIx.names[clampIdx(cur[focus], focusIx.n)]) : ''

  return (
    <>
      <ThumbPanel cols={cols} cur={cur} focus={focus} open={thumbsOpen} setOpen={setThumbsOpen} modKey={MOD_KEY} onPick={pick} onFocus={setFocus} onHover={hover} />

      <section aria-label="图片对比" className="col" style={{ flex: '1 1 0', minWidth: 0, minHeight: 0, gap: 10 }}>
        <div className="card row wrap" style={{ gap: '8px 16px', padding: '10px 14px', fontSize: 12, color: 'var(--ink-2)' }}>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">单击</kbd>仅切换该结果</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">{MOD_KEY} + 单击</kbd>按序号同步全部结果</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">Alt + 单击</kbd>按文件名编辑距离匹配</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">滚轮</kbd>同步缩放</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">拖动</kbd>同步平移</span>
          <span className="grow" />
          <span role="status" className="mono" style={{ color: 'var(--ink)' }}>{status || '点击缩略图切换图片'}</span>
        </div>

        <div className="row wrap" style={{ gap: 8 }}>
          <span className="row" style={{ gap: 0 }}>
            <button type="button" className="btn sm" onClick={() => step(-1)} disabled={atStart(cur, ids, lens)} style={{ borderRadius: '6px 0 0 6px' }}>‹ 上一张</button>
            <button type="button" className="btn sm" onClick={() => step(1)} disabled={atEnd(cur, ids, lens)} style={{ borderRadius: '0 6px 6px 0', borderLeft: 0 }}>下一张 ›</button>
          </span>
          <span className="row lbl" style={{ gap: 4 }}><kbd className="kbd">← ↑</kbd><kbd className="kbd">→ ↓</kbd>所有结果各自前进 / 后退一张，保持当前对齐</span>
          <span className="grow" />
          <button type="button" className="btn sm" onClick={() => zoomBy(1 / 1.5)} aria-label="缩小">−</button>
          <span ref={zoomRef} className="mono" style={{ minWidth: 52, textAlign: 'center', fontSize: 13 }}>100%</span>
          <button type="button" className="btn sm" onClick={() => zoomBy(1.5)} aria-label="放大">＋</button>
          <button type="button" className="btn sm" onClick={() => store.reset()}>适应窗口</button>
          <button type="button" className="btn sm" disabled={!n || copying} onClick={copyGrid}>{copying ? '复制中…' : '复制对比图'}</button>
        </div>

        {n === 0 && <div className="card empty">在左侧勾选至少一个推理结果</div>}

        <div ref={gridRef} className="cmp-grid" style={{ flex: '1 1 0', minHeight: 0, overflow: 'hidden', display: 'grid', gridTemplateRows: `repeat(${Math.max(1, Math.ceil(n / gridCols))}, minmax(0, 1fr))`, gridTemplateColumns: `repeat(${gridCols}, minmax(0, 1fr))`, gap: 10 }}>
          {ids.map((id) => {
            const st = states[id] ?? LOADING, ix = st.ix
            const i = ix?.n ? clampIdx(cur[id], ix.n) : 0
            const own = ix?.n ? serialOf(ix.names[i]) : ''
            return (
              <ImageCell key={id} id={id} name={displayName(id)} onRename={(value) => setAliases((a) => ({ ...a, [id]: value.trim() }))} color={colorOf(id)} focus={id === focus}
                mismatch={!!focusSerial && own !== '' && own !== focusSerial}
                count={ix?.n ? `${i + 1} / ${ix.n}` : st.loading ? '加载中' : '无图片'}
                ix={ix} i={i} peers={peers.get(id)!} store={store} cells={cells} nameOf={nameOf} onFocus={setFocus} onPrefetch={refreshPrefetch} />
            )
          })}
        </div>
      </section>
    </>
  )
}

export default memo(CompareImages)
