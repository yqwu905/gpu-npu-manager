import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { resultsApi } from '../api/results'
import { ErrorNote, MockBadge, SampleImage, Switch } from '../components/common'
import { useMock, usePoll } from '../lib/hooks'
import { fmtMetric, sampleKind, sampleText } from './Results'

// 三个系列的颜色在明度上拉开，色弱也能区分
const COLORS = ['#123F99', '#E08A2E', '#8DB3F0', '#2E8B57', '#8E2219']

/** 按编辑距离对齐，标出识别结果中与参考答案不一致的字符 */
function diffChars(pred: string, ref: string): { c: string; bad: boolean }[] {
  const a = Array.from(pred), b = Array.from(ref)
  const d = Array.from({ length: a.length + 1 }, (_, i) => Array.from({ length: b.length + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0)))
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
  const out: { c: string; bad: boolean }[] = []
  let i = a.length, j = b.length
  while (i > 0) {
    if (j > 0 && d[i][j] === d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)) {
      out.push({ c: a[i - 1], bad: a[i - 1] !== b[j - 1] }); i--; j--
    } else if (d[i][j] === d[i - 1][j] + 1) {
      out.push({ c: a[i - 1], bad: true }); i--
    } else j--
  }
  return out.reverse()
}

export default function ComparePage() {
  const [params, setParams] = useSearchParams()
  const ids = (params.get('ids') ?? '').split(',').map(Number).filter(Boolean)
  const sets = usePoll(() => resultsApi.list(), [], 0)
  const mock = useMock('results')
  const [baseIdx, setBaseIdx] = useState(0)
  const [sort, setSort] = useState<'spread' | 'worst' | 'none'>('spread')
  const [metricPick, setMetricPick] = useState('')
  const [page, setPage] = useState(0)
  const [showLq, setShowLq] = useState(true)
  const pageSize = 10
  const base = ids[Math.min(baseIdx, ids.length - 1)]

  const metrics = usePoll(() => (ids.length ? resultsApi.compareMetrics(ids) : Promise.resolve(null)), [ids.join(',')], 0)
  const m = metrics.data
  const metric = metricPick || m?.metrics[0]?.name || ''
  const metricDef = m?.metrics.find((x) => x.name === metric)
  // 接口按第一个结果集决定样本顺序和 asc/desc 排序，所以把基线放在最前面
  const ordered = [base, ...ids.filter((id) => id !== base)]
  const apiSort = sort === 'worst' ? (metricDef?.higher_is_better ? 'asc' : 'desc') : 'spread'
  const samples = usePoll(
    () => (ids.length >= 2 ? resultsApi.compareSamples(ordered, sort === 'none' || !metric ? undefined : metric, apiSort, page * pageSize, pageSize) : Promise.resolve(null)),
    [ordered.join(','), metric, sort, page], 0,
  )
  const sampleItems = samples.data?.items ?? []
  const kind = sampleKind(sampleItems.flatMap((it) => Object.values(it.results)))
  const pages = Math.max(1, Math.ceil((samples.data?.total ?? 0) / pageSize))

  const setIds = (next: number[]) => {
    setBaseIdx(0)
    setPage(0)
    setParams(next.length ? { ids: next.join(',') } : {}, { replace: true })
  }
  const candidates = sets.data ?? []
  const nameOf = (id: number) => m?.result_sets.find((r) => r.id === id)?.name ?? sets.data?.find((r) => r.id === id)?.name ?? `#${id}`
  const colorOf = (id: number) => COLORS[ids.indexOf(id) % COLORS.length]

  const valueOf = (id: number, name: string) => m?.values[String(id)]?.[name] ?? null
  // LQ 基线：取基线结果集的，没有时取其他结果集的（同一配置的 LQ 指标相同）
  const lqOwner = [base, ...ids].find((id) => m?.lq_values?.[String(id)])
  const lqValues = lqOwner !== undefined ? m?.lq_values[String(lqOwner)] : undefined
  const hasLqImage = sampleItems.some((it) => Object.values(it.results).some((r) => typeof r?.lq_image === 'string'))
  const lqCols = hasLqImage && showLq ? 1 : 0
  const bestOf = (name: string, hi: boolean) => {
    const vs = ids.map((id) => valueOf(id, name)).filter((v): v is number => v !== null)
    return vs.length ? (hi ? Math.max(...vs) : Math.min(...vs)) : null
  }
  const deltaText = (v: number | null, b: number | null) => {
    if (v === null || b === null) return '-'
    const dv = v - b
    return `${dv > 0 ? '+' : dv < 0 ? '−' : '±'}${fmtMetric(Math.abs(dv))}`
  }
  const deltaColor = (v: number | null, b: number | null, hi: boolean) => (v === null || b === null || v === b ? 'var(--muted)' : (v > b) === hi ? '#1B5E3A' : '#A1281F')

  return (
    <main className="main" style={{ gap: 18 }}>
      <header className="page-head">
        <div className="grow">
          <h1>指标对比</h1>
          <div className="lbl" style={{ marginTop: 4 }}>{ids.length ? `${ids.length} 个结果集` : '选择两个以上同类结果集进行对比'}</div>
        </div>
        <MockBadge show={mock} what="对比" />
      </header>

      <div className="row wrap" style={{ gap: 8 }}>
        {ids.map((id, i) => (
          <span key={id} className="row" style={{ gap: 8, height: 34, padding: '0 6px 0 12px', borderRadius: 17, background: '#FFFFFF', border: '1px solid var(--border)' }}>
            <span className="dot" style={{ width: 10, height: 10, borderRadius: 5, background: colorOf(id) }} />
            <span className="mono" style={{ fontSize: 13 }}>{nameOf(id)}</span>
            {id === base && <span className="badge" style={{ background: 'var(--ink)', color: '#FFFFFF' }}>基线</span>}
            <button type="button" aria-label={`移除 ${nameOf(id)}`} onClick={() => setIds(ids.filter((_, k) => k !== i))}
              style={{ width: 24, height: 24, border: 0, borderRadius: 12, background: 'transparent', cursor: 'pointer', color: 'var(--muted)' }}>×</button>
          </span>
        ))}
        <select className="inp" aria-label="添加结果集" value="" onChange={(e) => e.target.value && setIds([...ids, Number(e.target.value)])} style={{ height: 34 }}>
          <option value="">＋ 添加结果集</option>
          {candidates.filter((r) => !ids.includes(r.id)).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        <Link to="/results" style={{ fontSize: 13 }}>去结果列表挑选</Link>
      </div>
      <ErrorNote error={metrics.error || samples.error} />

      {ids.length < 2 && <div className="card empty">至少选择两个结果集。可以在上面添加，或者在“结果与评测”页勾选后点“对比所选”。</div>}

      {ids.length >= 2 && m && (
        <>
          <section className="card table-box">
            <table className="tbl">
              <thead><tr>
                <th>基线</th><th>结果集</th>
                {m.metrics.map((d) => <th key={d.name} className="num" title={d.description}>{d.label} <span style={{ color: 'var(--faint)' }}>{d.higher_is_better ? '↑' : '↓'}</span></th>)}
                <th>模型 · 数据集</th>
              </tr></thead>
              <tbody>
                {ids.map((id, i) => (
                  <tr key={id}>
                    <td><input type="radio" name="baseline" aria-label={`设为基线 ${nameOf(id)}`} checked={id === base} onChange={() => setBaseIdx(i)} style={{ width: 16, height: 16 }} /></td>
                    <td><span className="row"><span className="dot" style={{ width: 10, height: 10, borderRadius: 5, background: colorOf(id) }} /><span className="mono" style={{ fontWeight: 500 }}>{nameOf(id)}</span></span></td>
                    {m.metrics.map((d) => {
                      const v = valueOf(id, d.name), b = valueOf(base, d.name)
                      return (
                        <td key={d.name} className="num">
                          <div className="mono" style={{ fontSize: 14, fontWeight: v !== null && v === bestOf(d.name, d.higher_is_better) ? 600 : 400 }}>{fmtMetric(v)}</div>
                          <div className="mono" style={{ fontSize: 12, color: id === base ? 'var(--muted)' : deltaColor(v, b, d.higher_is_better) }}>{id === base ? '基线' : deltaText(v, b)}</div>
                        </td>
                      )
                    })}
                    <td className="lbl">{(() => { const meta = m.result_sets.find((r) => r.id === id)?.meta ?? {}; return `${String(meta.model ?? '-')} · ${String(meta.dataset ?? '-')}` })()}</td>
                  </tr>
                ))}
                {lqValues && (
                  <tr>
                    <td />
                    <td><span className="row"><span className="dot" style={{ width: 10, height: 10, borderRadius: 5, background: 'var(--faint)' }} /><span style={{ fontWeight: 500 }}>LQ 基线</span></span></td>
                    {m.metrics.map((d) => {
                      const v = lqValues[d.name] ?? null, b = valueOf(base, d.name)
                      return (
                        <td key={d.name} className="num">
                          <div className="mono" style={{ fontSize: 14, color: 'var(--ink-2)' }}>{fmtMetric(v)}</div>
                          <div className="mono" style={{ fontSize: 12, color: deltaColor(v, b, d.higher_is_better) }}>{deltaText(v, b)}</div>
                        </td>
                      )
                    })}
                    <td className="lbl">LQ 图片与同样的参考值计算</td>
                  </tr>
                )}
              </tbody>
            </table>
            {m.metrics.length === 0 && <div className="empty">所选结果集都还没有评测指标，可以先在“结果与评测”页发起评测</div>}
          </section>

          <section style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 14 }}>
            {m.metrics.map((d) => {
              const vs = ids.map((id) => valueOf(id, d.name))
              const nums = vs.filter((v): v is number => v !== null)
              if (!nums.length) return null
              const mn = Math.min(...nums), mx = Math.max(...nums), pad = (mx - mn) * 0.25 || Math.abs(mx) * 0.01 || 0.01
              const lo = mn - pad, hi = mx + pad
              const pos = (v: number) => ((v - lo) / (hi - lo)) * 100
              const best = bestOf(d.name, d.higher_is_better)
              const bv = valueOf(base, d.name)
              return (
                <div key={d.name} className="card col" style={{ padding: '16px 18px', gap: 10 }}>
                  <div className="row" style={{ alignItems: 'baseline' }}>
                    <span style={{ fontWeight: 600 }}>{d.label}</span>
                    <span className="lbl grow">{d.higher_is_better ? '越高越好' : '越低越好'}</span>
                    <span className="lbl">最佳 <span className="mono" style={{ color: 'var(--ink)' }}>{nameOf(ids[vs.indexOf(best)])}</span></span>
                  </div>
                  <div className="col" style={{ gap: 10, paddingTop: 4 }}>
                    {ids.map((id, i) => {
                      const v = vs[i]
                      return (
                        <div key={id} className="row" style={{ gap: 10 }}>
                          <span className="mono lbl ellipsis" style={{ width: 120 }}>{nameOf(id)}</span>
                          <div className="grow" style={{ position: 'relative', height: 18 }}>
                            <div style={{ position: 'absolute', left: 0, right: 0, top: 8, height: 2, background: 'var(--track)' }} />
                            {v !== null && bv !== null && (
                              <div style={{ position: 'absolute', top: 8, height: 2, left: `${Math.min(pos(v), pos(bv))}%`, width: `${Math.abs(pos(v) - pos(bv))}%`, background: colorOf(id), opacity: 0.45 }} />
                            )}
                            {v !== null && (
                              <div style={{ position: 'absolute', top: 3, width: 12, height: 12, borderRadius: 6, marginLeft: -6, left: `${pos(v)}%`, background: colorOf(id), border: '2px solid #FFFFFF' }} />
                            )}
                          </div>
                          <span className="mono" style={{ width: 56, textAlign: 'right', fontSize: 13, fontWeight: v === best ? 600 : 400 }}>{fmtMetric(v)}</span>
                        </div>
                      )
                    })}
                  </div>
                  <div className="row mono lbl" style={{ justifyContent: 'space-between', paddingLeft: 130, paddingRight: 66 }}><span>{fmtMetric(lo)}</span><span>{fmtMetric(hi)}</span></div>
                </div>
              )
            })}
          </section>
          <div className="lbl" style={{ marginTop: -8 }}>点图坐标轴按每个指标的取值范围缩放，不从 0 开始；与基线之间的线段表示差距。</div>

          <section className="card col">
            <div className="row wrap" style={{ padding: '16px 18px', gap: 12, borderBottom: '1px solid var(--border-soft)' }}>
              <div className="grow">
                <h2>样本对比</h2>
                <div className="lbl" style={{ marginTop: 4 }}>同一样本 id 下并排展示各结果集的输出，按逐样本指标差异排序，快速找到差别最大的样本</div>
              </div>
              {hasLqImage && <span className="row" style={{ gap: 6 }}><span className="lbl">LQ</span><Switch on={showLq} onChange={setShowLq} label="并排显示 LQ" /></span>}
              {m.metrics.length > 0 && (
                <>
                  <label className="row"><span className="lbl">指标</span>
                    <select className="inp" value={metric} onChange={(e) => { setMetricPick(e.target.value); setPage(0) }}>
                      {m.metrics.map((d) => <option key={d.name} value={d.name}>{d.label}</option>)}
                    </select>
                  </label>
                  <label className="row"><span className="lbl">排序</span>
                    <select className="inp" value={sort} onChange={(e) => { setSort(e.target.value as typeof sort); setPage(0) }}>
                      <option value="spread">差异最大优先</option>
                      <option value="worst">基线最差优先</option>
                      <option value="none">原始顺序</option>
                    </select>
                  </label>
                </>
              )}
              <span className="row" style={{ gap: 6 }}>
                <button type="button" className="btn sm" aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
                <span className="mono lbl">{page + 1} / {pages}</span>
                <button type="button" className="btn sm" aria-label="下一页" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>›</button>
              </span>
            </div>

            {sampleItems.map((it) => {
              const vals = ids.map((id) => it.results[String(id)]?.metrics?.[metric] ?? null)
              const nums = vals.filter((v): v is number => v !== null)
              const best = nums.length ? (metricDef?.higher_is_better ? Math.max(...nums) : Math.min(...nums)) : null
              const bv = it.results[String(base)]?.metrics?.[metric] ?? null
              // 参考值取基线结果集的 ref_image / ref_text，没有时取其他结果集的
              const refOf = (k: 'ref_image' | 'ref_text') => [base, ...ids].map((id) => it.results[String(id)]?.[k]).find((v) => typeof v === 'string')
              const refImage = refOf('ref_image')
              const refOwner = ids.find((id) => it.results[String(id)]?.ref_image === refImage) ?? base
              // 评测补上的 ref_image / lq_image 在评测服务器上，按 media 带上评测 ID
              const mediaOf = (owner: number, k: 'ref_image' | 'lq_image') => it.results[String(owner)]?.media?.[k]
              const lqHolder = [base, ...ids].find((id) => typeof it.results[String(id)]?.lq_image === 'string')
              const lqImage = lqHolder !== undefined ? String(it.results[String(lqHolder)]?.lq_image) : null
              const spreadLabel = metricDef ? <span className="lbl">{metricDef.label} 差 <span className="mono" style={{ color: 'var(--ink)' }}>{fmtMetric(it.spread)}</span></span> : null
              if (kind === 'image') {
                return (
                  <div key={it.id} className="table-box" style={{ borderBottom: '1px solid var(--border-soft)' }}>
                    <div style={{ display: 'grid', gridTemplateColumns: `110px repeat(${ids.length + 1 + lqCols}, minmax(150px, 1fr))`, gap: 12, padding: '12px 18px', alignItems: 'start', minWidth: 110 + (ids.length + 1 + lqCols) * 162 }}>
                      <div className="col" style={{ gap: 4 }}>
                        <span className="mono" style={{ fontWeight: 500 }}>{it.id}</span>
                        {spreadLabel}
                      </div>
                      {lqCols > 0 && (
                        <div className="col" style={{ gap: 6 }}>
                          {lqImage && lqHolder !== undefined ? <SampleImage src={resultsApi.fileUrl(lqHolder, lqImage, mediaOf(lqHolder, 'lq_image'))} seed={it.id} mock={mock} blur={2} label={`LQ ${it.id}`} /> : <div className="thumb" />}
                          <span className="lbl">LQ</span>
                        </div>
                      )}
                      <div className="col" style={{ gap: 6 }}>
                        {refImage ? <SampleImage src={resultsApi.fileUrl(refOwner, refImage, mediaOf(refOwner, 'ref_image'))} seed={it.id} mock={mock} label={`参考图 ${it.id}`} /> : <div className="thumb" />}
                        <span className="lbl">参考图</span>
                      </div>
                      {ids.map((id, i) => {
                        const r = it.results[String(id)]
                        const img = typeof r?.image === 'string' ? r.image : null
                        const v = vals[i]
                        return (
                          <div key={id} className="col" style={{ gap: 6 }}>
                            {img ? <SampleImage src={resultsApi.fileUrl(id, img)} seed={it.id} mock={mock} blur={[1.6, 0.8, 0.5][i] ?? 0.5} label={`${nameOf(id)} 输出 ${it.id}`}
                              style={{ boxShadow: v !== null && v === best ? '0 0 0 2px #2E8B57' : undefined }} /> : <div className="thumb" />}
                            <span className="row mono" style={{ fontSize: 12, gap: 6 }}>
                              <span className="dot" style={{ background: colorOf(id) }} />
                              <span style={{ fontWeight: v === best ? 600 : 400 }}>{fmtMetric(v)}</span>
                              {id !== base && <span style={{ color: deltaColor(v, bv, !!metricDef?.higher_is_better) }}>{deltaText(v, bv)}</span>}
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              }
              const ref = refOf('ref_text') ?? ''
              const crop = it.results[String(base)]?.image
              return (
                <div key={it.id} className="col" style={{ padding: '14px 18px', borderBottom: '1px solid var(--border-soft)', gap: 8 }}>
                  <div className="row" style={{ gap: 12 }}>
                    <span className="mono" style={{ fontWeight: 500 }}>{it.id}</span>
                    {spreadLabel}
                    <span className="grow" />
                    {lqCols > 0 && lqImage && lqHolder !== undefined && <SampleImage src={resultsApi.fileUrl(lqHolder, lqImage, mediaOf(lqHolder, 'lq_image'))} seed={it.id} mock={mock} blur={2} label={`LQ ${it.id}`} style={{ width: 180, aspectRatio: '7 / 1' }} />}
                    {typeof crop === 'string' && <SampleImage src={resultsApi.fileUrl(base, crop)} seed={it.id} mock={mock} label={`样本图片 ${it.id}`} style={{ width: 180, aspectRatio: '7 / 1' }} />}
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '170px minmax(0, 1fr) 70px', gap: '6px 12px', alignItems: 'center', fontSize: 15 }}>
                    <span className="lbl">参考答案</span><span>{ref}</span><span />
                    {ids.map((id, i) => {
                      const r = it.results[String(id)]
                      const text = sampleText(r) ?? ''
                      return [
                        <span key={`n${id}`} className="row" style={{ gap: 6, fontSize: 12 }}><span className="dot" style={{ background: colorOf(id) }} /><span className="mono ellipsis">{nameOf(id)}</span></span>,
                        <span key={`t${id}`}>{!r ? <span className="lbl">该结果集没有这个样本</span> : !ref ? text : diffChars(text, ref).map((ch, k) => (
                          <span key={k} style={ch.bad ? { background: '#FBE1DF', color: '#8E2219', textDecoration: 'underline', borderRadius: 2 } : undefined}>{ch.c}</span>
                        ))}</span>,
                        <span key={`v${id}`} className="mono" style={{ fontSize: 12, textAlign: 'right', color: vals[i] !== null && vals[i] !== best ? '#8E2219' : 'var(--muted)' }}>{fmtMetric(vals[i])}</span>,
                      ]
                    })}
                  </div>
                </div>
              )
            })}
            {!samples.loading && (samples.data?.items.length ?? 0) === 0 && <div className="empty">没有样本</div>}
            <div className="lbl" style={{ padding: '12px 18px' }}>{kind === 'image' ? '绿色描边为该样本指标最好的结果' : '红色下划线标出与参考答案不一致的字符'}{m.metrics.length === 0 ? '；评测后可以按逐样本指标排序' : ''}</div>
          </section>
        </>
      )}
    </main>
  )
}
