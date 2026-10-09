import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { resultsApi } from '../api/results'
import type { Sample } from '../api/types'
import { SampleImage } from '../components/common'

/** 结果集中的一张图片，按文件名字母序排列 */
interface ImageFile { name: string; path: string; url: string }
interface ImageList { files: ImageFile[]; loading: boolean; error: string | null }

const PAGE = 500
// Mac 上 Ctrl+左键会被系统当成右键，修饰键改用 ⌘；其他平台用 Ctrl
const IS_MAC = /mac/i.test((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform || navigator.platform || '')
export const MOD_KEY = IS_MAC ? '⌘' : 'Ctrl'
const isMod = (e: { ctrlKey: boolean; metaKey: boolean }) => (IS_MAC ? e.metaKey : e.ctrlKey)
const MAX_ZOOM = 16

const baseName = (p: string) => p.split(/[\\/]/).pop() || p
const byName = (a: ImageFile, b: ImageFile) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.path < b.path ? -1 : a.path > b.path ? 1 : 0)

/** 文件名的编辑距离，用于 Alt+单击 匹配最接近的图片 */
function editDistance(a: string, b: string): number {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    const row = [i]
    for (let j = 1; j <= b.length; j++) row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    prev = row
  }
  return prev[b.length]
}

/** 文件名中最长的一段数字，用来提示各结果当前显示的是否是同一编号的图片 */
const serialOf = (name: string) => (name.match(/\d+/g) ?? []).reduce((best, s) => (s.length > best.length ? s : best), '')

/** 分页读取结果集的全部样本，取出 image 字段并按文件名排序；已读过的结果集不重复读取 */
function useImageLists(ids: number[]) {
  const [lists, setLists] = useState<Record<number, ImageList>>({})
  const started = useRef(new Set<number>())
  useEffect(() => {
    for (const id of ids) {
      if (started.current.has(id)) continue
      started.current.add(id)
      setLists((m) => ({ ...m, [id]: { files: [], loading: true, error: null } }))
      ;(async () => {
        const samples: Sample[] = []
        try {
          for (let offset = 0; ; offset += PAGE) {
            const page = await resultsApi.samples(id, offset, PAGE)
            samples.push(...page.items)
            if (!page.items.length || offset + PAGE >= page.total) break
          }
          const files = samples
            .filter((s) => typeof s.image === 'string')
            .map((s) => ({ name: baseName(String(s.image)), path: String(s.image), url: resultsApi.sampleFileUrl(id, s, 'image') }))
            .sort(byName)
          setLists((m) => ({ ...m, [id]: { files, loading: false, error: null } }))
        } catch (e) {
          started.current.delete(id)
          setLists((m) => ({ ...m, [id]: { files: [], loading: false, error: e instanceof Error ? e.message : String(e) } }))
        }
      })()
    }
  }, [ids.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps
  return lists
}

interface View { z: number; x: number; y: number }
// 所有图片共用同一组缩放与平移；平移限制在图片层仍铺满视口的范围内
const clampView = (z: number, x: number, y: number, w: number, h: number): View =>
  ({ z, x: Math.min(0, Math.max(w - w * z, x)), y: Math.min(0, Math.max(h - h * z, y)) })
const zoomAt = (v: View, factor: number, cx: number, cy: number, w: number, h: number): View => {
  const z = Math.min(MAX_ZOOM, Math.max(1, v.z * factor))
  const k = z / v.z
  return clampView(z, cx - (cx - v.x) * k, cy - (cy - v.y) * k, w, h)
}

interface Props {
  ids: number[]
  nameOf: (id: number) => string
  colorOf: (id: number) => string
  mock: boolean
}

export default function CompareImages({ ids, nameOf, colorOf, mock }: Props) {
  const lists = useImageLists(ids)
  const [cur, setCur] = useState<Record<number, number>>({})
  const [focusPick, setFocus] = useState<number | null>(null)
  const [hold, setHold] = useState<{ cell: number; other: number } | null>(null)
  const [status, setStatus] = useState('')
  const [thumbsOpen, setThumbsOpen] = useState(true)
  const [view, setView] = useState<View>({ z: 1, x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{ x: number; y: number; v: View; w: number; h: number } | null>(null)
  const gridRef = useRef<HTMLDivElement | null>(null)

  const filesOf = (id: number) => lists[id]?.files ?? []
  const idxOf = (id: number) => Math.min(cur[id] ?? 0, Math.max(0, filesOf(id).length - 1))
  const fileOf = (id: number): ImageFile | undefined => filesOf(id)[idxOf(id)]
  const focus = focusPick !== null && ids.includes(focusPick) ? focusPick : ids[0]

  const pick = (id: number, i: number, e: ReactMouseEvent) => {
    const name = filesOf(id)[i]?.name ?? ''
    if (isMod(e)) {
      setCur((c) => ({ ...c, ...Object.fromEntries(ids.map((x) => [x, Math.min(i, Math.max(0, filesOf(x).length - 1))])) }))
      setStatus(`${MOD_KEY} 按序号同步 · 第 ${i + 1} 张`)
    } else if (e.altKey) {
      e.preventDefault()
      const next: Record<number, number> = { [id]: i }
      for (const x of ids) {
        if (x === id) continue
        let best = 0, bd = Infinity
        filesOf(x).forEach((f, k) => { const d = editDistance(name, f.name); if (d < bd) { bd = d; best = k } })
        next[x] = best
      }
      setCur((c) => ({ ...c, ...next }))
      setStatus(`Alt 文件名匹配 · ${name}`)
    } else {
      setCur((c) => ({ ...c, [id]: i }))
      setStatus(`仅切换 ${nameOf(id)} · ${name}`)
    }
    setFocus(id)
  }

  const step = useCallback((d: number) => {
    setCur((c) => ({ ...c, ...Object.fromEntries(ids.map((x) => [x, Math.min(Math.max(0, (lists[x]?.files.length ?? 0) - 1), Math.max(0, Math.min(c[x] ?? 0, (lists[x]?.files.length ?? 1) - 1) + d))])) }))
    setStatus(`${d < 0 ? '上一张' : '下一张'} · 全部结果`)
  }, [ids.join(','), lists]) // eslint-disable-line react-hooks/exhaustive-deps

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

  // React 的 onWheel 是 passive 监听，无法阻止浏览器自带的 Ctrl+滚轮页面缩放，所以挂原生监听
  useEffect(() => {
    const el = gridRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      // Mac 触控板双指捏合以 ctrlKey=true 的滚轮事件送达，一并当作缩放
      if (!isMod(e) && !(IS_MAC && e.ctrlKey)) return
      const vp = (e.target as HTMLElement).closest?.('[data-viewport]')
      if (!vp) return
      e.preventDefault()
      const r = vp.getBoundingClientRect()
      setView((v) => zoomAt(v, Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top, r.width, r.height))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const zoomBy = (factor: number) => {
    const r = gridRef.current?.querySelector('[data-viewport]')?.getBoundingClientRect()
    if (!r) return
    setView((v) => zoomAt(v, factor, r.width / 2, r.height / 2, r.width, r.height))
  }

  const panStart = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!isMod(e) || e.button !== 0 || (e.target as HTMLElement).closest('button')) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    const r = e.currentTarget.getBoundingClientRect()
    drag.current = { x: e.clientX, y: e.clientY, v: view, w: r.width, h: r.height }
    setDragging(true)
  }
  const panMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (d) setView(clampView(d.v.z, d.v.x + e.clientX - d.x, d.v.y + e.clientY - d.y, d.w, d.h))
  }
  const panEnd = () => { if (drag.current) { drag.current = null; setDragging(false) } }

  const holdStart = (cell: number, other: number) => setHold({ cell, other })
  const holdEnd = () => setHold(null)
  const holdKey = (cell: number, other: number) => (e: ReactKeyboardEvent) => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); if (!hold) holdStart(cell, other) }
  }

  const n = ids.length
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : 3
  const atStart = ids.every((id) => idxOf(id) === 0)
  const atEnd = ids.every((id) => idxOf(id) >= filesOf(id).length - 1)
  const focusSerial = (() => { const f = fileOf(focus); return f ? serialOf(f.name) : '' })()
  const transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`
  const thumbW = Math.min(Math.max(n, 1), 4) * 120 + 10

  return (
    <>
      <div className="cmp-fold" style={{ flexBasis: thumbsOpen ? thumbW : 44 }}>
        <section aria-label="缩略图" aria-hidden={!thumbsOpen} className={thumbsOpen ? 'card cmp-panel' : 'card cmp-panel off'} style={{ width: thumbW }}>
          <div className="cmp-panel-head">
            <h2 className="grow" style={{ fontSize: 15 }}>缩略图</h2>
            <button type="button" className="cmp-icon-btn" onClick={() => setThumbsOpen(false)} aria-label="折叠缩略图栏" aria-expanded="true">‹</button>
          </div>
          <div style={{ flex: '1 1 0', minHeight: 0, overflow: 'auto' }}>
            <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.max(n, 1)}, 112px)`, gap: 8, padding: '0 8px 8px', alignItems: 'start' }}>
              {ids.map((id) => {
                const list = lists[id]
                const files = list?.files ?? []
                return (
                  <div key={id} className="col" style={{ gap: 6, minWidth: 0 }}>
                    <button type="button" className="cmp-col-head" onClick={() => setFocus(id)} aria-label={`以 ${nameOf(id)} 为对齐基准`} style={{ borderBottomColor: colorOf(id) }}>
                      <span className="row" style={{ gap: 6, minWidth: 0, width: '100%' }}>
                        <span className="dot" style={{ width: 9, height: 9, background: colorOf(id) }} />
                        <span className="mono ellipsis" style={{ fontSize: 12, fontWeight: id === focus ? 600 : 400 }}>{nameOf(id)}</span>
                      </span>
                      <span className="mono lbl" style={{ fontSize: 11 }}>{files.length ? `${idxOf(id) + 1} / ${files.length}` : list?.loading ? '加载中' : '0 张'}</span>
                    </button>
                    {list?.error && <span className="lbl" style={{ color: '#A1281F' }}>{list.error}</span>}
                    {files.map((f, i) => {
                      const on = idxOf(id) === i
                      return (
                        <button key={f.path} type="button" className="cmp-thumb" onClick={(e) => pick(id, i, e)} aria-current={on} aria-label={`${nameOf(id)} ${f.name}`}
                          title={`${f.name}\n单击：仅切换该结果\n${MOD_KEY}+单击：按序号同步全部\nAlt+单击：按文件名匹配`}
                          style={on ? { background: 'var(--bg)', boxShadow: `inset 0 0 0 2px ${colorOf(id)}` } : undefined}>
                          <SampleImage src={f.url} seed={f.name} mock={mock} label={f.name} style={{ borderRadius: 4 }} />
                          <span className="row" style={{ gap: 4, width: '100%', minWidth: 0 }}>
                            <span className="mono" style={{ fontSize: 10, color: 'var(--faint)', flexShrink: 0 }}>{i + 1}</span>
                            <span className="mono ellipsis" style={{ fontSize: 11 }}>{f.name}</span>
                          </span>
                        </button>
                      )
                    })}
                  </div>
                )
              })}
            </div>
          </div>
        </section>
        <button type="button" className={thumbsOpen ? 'cmp-rail off' : 'cmp-rail'} onClick={() => setThumbsOpen(true)} aria-label="展开缩略图栏" aria-expanded="false">
          <span aria-hidden="true">›</span>
          <span className="cmp-rail-title">缩略图</span>
        </button>
      </div>

      <section aria-label="图片对比" className="col" style={{ flex: '1 1 0', minWidth: 0, minHeight: 0, gap: 10 }}>
        <div className="card row wrap" style={{ gap: '8px 16px', padding: '10px 14px', fontSize: 12, color: 'var(--ink-2)' }}>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">单击</kbd>仅切换该结果</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">{MOD_KEY} + 单击</kbd>按序号同步全部结果</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">Alt + 单击</kbd>按文件名编辑距离匹配</span>
          <span className="row" style={{ gap: 6 }}><kbd className="kbd">{MOD_KEY} + 滚轮 / 拖动</kbd>同步缩放与平移</span>
          <span className="grow" />
          <span role="status" className="mono" style={{ color: 'var(--ink)' }}>{status || '点击缩略图切换图片'}</span>
        </div>

        <div className="row wrap" style={{ gap: 8 }}>
          <span className="row" style={{ gap: 0 }}>
            <button type="button" className="btn sm" onClick={() => step(-1)} disabled={atStart} style={{ borderRadius: '6px 0 0 6px' }}>‹ 上一张</button>
            <button type="button" className="btn sm" onClick={() => step(1)} disabled={atEnd} style={{ borderRadius: '0 6px 6px 0', borderLeft: 0 }}>下一张 ›</button>
          </span>
          <span className="row lbl" style={{ gap: 4 }}><kbd className="kbd">← ↑</kbd><kbd className="kbd">→ ↓</kbd>所有结果各自前进 / 后退一张，保持当前对齐</span>
          <span className="grow" />
          <button type="button" className="btn sm" onClick={() => zoomBy(1 / 1.5)} aria-label="缩小">−</button>
          <span className="mono" style={{ minWidth: 52, textAlign: 'center', fontSize: 13 }}>{Math.round(view.z * 100)}%</span>
          <button type="button" className="btn sm" onClick={() => zoomBy(1.5)} aria-label="放大">＋</button>
          <button type="button" className="btn sm" onClick={() => setView({ z: 1, x: 0, y: 0 })}>适应窗口</button>
        </div>

        {n === 0 && <div className="card empty">在左侧勾选至少一个推理结果</div>}

        <div ref={gridRef} style={{ flex: '1 1 0', minHeight: 0, overflowY: 'auto', display: 'grid', gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap: 10, alignContent: 'start' }}>
          {ids.map((id) => {
            const holding = hold?.cell === id
            const shown = holding ? hold.other : id
            const file = fileOf(shown)
            const own = fileOf(id)
            const mismatch = !!own && !!focusSerial && serialOf(own.name) !== '' && serialOf(own.name) !== focusSerial
            return (
              <figure key={id} className="card cmp-cell" style={{ boxShadow: id === focus ? `0 0 0 2px ${colorOf(id)}` : undefined }}>
                <figcaption className="row" style={{ gap: 8, padding: '8px 10px', borderTop: `3px solid ${colorOf(id)}`, borderBottom: '1px solid var(--border-soft)' }}>
                  <button type="button" className="cmp-name-btn" onClick={() => setFocus(id)} aria-label={`以 ${nameOf(id)} 为对齐基准`}>
                    <span className="dot" style={{ width: 10, height: 10, background: colorOf(id) }} />
                    <span className="mono ellipsis" style={{ fontSize: 13, fontWeight: 500 }}>{nameOf(id)}</span>
                  </button>
                  <span className="grow" />
                  {mismatch && <span className="badge" style={{ background: '#FFF4D6', color: '#5C3F00' }}>编号不一致</span>}
                  <span className="mono lbl">{filesOf(id).length ? `${idxOf(id) + 1} / ${filesOf(id).length}` : lists[id]?.loading ? '加载中' : '无图片'}</span>
                </figcaption>
                <div data-viewport="1" className="cmp-vp" onPointerDown={panStart} onPointerMove={panMove} onPointerUp={panEnd} onPointerCancel={panEnd}
                  style={{ cursor: dragging ? 'grabbing' : view.z > 1 ? 'grab' : undefined }}>
                  <div className="cmp-layer" style={{ transform }}>
                    {file && (mock
                      ? <SampleImage src={file.url} seed={file.name} mock label={`${nameOf(shown)} ${file.name}`} style={{ width: '100%', height: '100%', borderRadius: 0, aspectRatio: 'auto' }} />
                      : <img src={file.url} alt={`${nameOf(shown)} ${file.name}`} draggable={false} style={{ imageRendering: view.z > 1 ? 'pixelated' : undefined }} />)}
                  </div>
                  {holding && (
                    <div className="cmp-holding">
                      <span className="dot" style={{ background: colorOf(shown) }} />
                      正在显示 {nameOf(shown)}
                    </div>
                  )}
                  <div className="cmp-hold-btns">
                    {ids.filter((o) => o !== id).map((o) => (
                      <button key={o} type="button" className={holding && hold.other === o ? 'cmp-hold on' : 'cmp-hold'}
                        onPointerDown={() => holdStart(id, o)} onPointerUp={holdEnd} onPointerLeave={holdEnd}
                        onKeyDown={holdKey(id, o)} onKeyUp={holdEnd} onBlur={holdEnd}
                        aria-label={`按住查看 ${nameOf(o)} 的当前图`}>
                        <span className="dot" style={{ width: 10, height: 10, background: colorOf(o), boxShadow: '0 0 0 1.5px #FFFFFF' }} />
                        <span className="ellipsis" style={{ maxWidth: 120 }}>{nameOf(o)}</span>
                      </button>
                    ))}
                  </div>
                </div>
                <div className="row" style={{ gap: 8, padding: '8px 10px' }}>
                  <span className="mono ellipsis grow" style={{ fontSize: 12 }} title={file?.path}>{file?.name ?? '-'}</span>
                  {n > 1 && <span className="lbl" style={{ whiteSpace: 'nowrap' }}>按住右下角按钮切换</span>}
                </div>
              </figure>
            )
          })}
        </div>
      </section>
    </>
  )
}
