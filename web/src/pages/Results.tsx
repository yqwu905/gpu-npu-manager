import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { resultsApi } from '../api/results'
import { serversApi } from '../api/servers'
import type { Evaluator, MetricKind, ResultSet, Sample } from '../api/types'
import { Badge, ErrorNote, MockBadge, Modal, SampleImage } from '../components/common'
import { EVAL_LOOK, relTime } from '../lib/format'
import { useMock, usePoll } from '../lib/hooks'

const SET_LOOK = {
  none: { name: '未评测', bg: '#ECEDEB', fg: '#3A4048' },
  running: { name: '评测中', bg: '#DCE7FC', fg: '#123F99' },
  done: { name: '已评测', bg: '#E3F2E8', fg: '#1B5E3A' },
}
const setState = (r: ResultSet) => (r.evaluating ? 'running' : Object.keys(r.metrics).length ? 'done' : 'none')

export const fmtMetric = (v: number | null | undefined) => (v === null || v === undefined ? '-' : Math.abs(v) >= 10 ? v.toFixed(2) : v.toFixed(3))

/** 指标名 -> 显示名，例如 ned -> 1-NED */
export const metricLabel = (evaluators: Evaluator[] | null | undefined, name: string) => evaluators?.find((e) => e.name === name)?.label ?? name

/** 样本的展示方式：有 text 字段按文字展示，否则有 image 字段按图片展示 */
export function sampleKind(items: (Sample | null | undefined)[]): MetricKind | null {
  if (items.some((s) => typeof s?.text === 'string')) return 'text'
  if (items.some((s) => typeof s?.image === 'string')) return 'image'
  return null
}

export default function ResultsPage() {
  const navigate = useNavigate()
  const [sel, setSel] = useState<number[]>([])
  const [focus, setFocus] = useState<number | null>(null)
  const [evalFor, setEvalFor] = useState<ResultSet | null>(null)
  const [showReg, setShowReg] = useState(false)
  const sets = usePoll(() => resultsApi.list(), [], 15_000)
  const evaluators = usePoll(() => resultsApi.evaluators(), [], 0)
  const evals = usePoll(() => resultsApi.evaluations(), [], 10_000)
  const mock = useMock('results')

  const list = sets.data ?? []
  const evs = evaluators.data ?? []
  // 只显示至少一个结果集有值的指标列
  const metricDefs = evs.filter((m) => list.some((r) => typeof r.metrics[m.name] === 'number'))
  const best = Object.fromEntries(metricDefs.map((m) => {
    const vs = list.map((r) => r.metrics[m.name]).filter((v): v is number => typeof v === 'number')
    return [m.name, vs.length ? (m.higher_is_better ? Math.max(...vs) : Math.min(...vs)) : null]
  }))
  const focused = list.find((r) => r.id === focus) ?? list[0]
  const selSets = sel.map((id) => list.find((r) => r.id === id)).filter(Boolean) as ResultSet[]
  const toggle = (id: number) => setSel(sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id])

  return (
    <main className="main">
      <header className="page-head">
        <div className="grow">
          <h1>结果与评测</h1>
          <div className="lbl" style={{ marginTop: 4 }}>结果集留在产生它的服务器上，通过 Agent 读取；一个结果集是一个目录：有 predictions.jsonl 时按它读取，没有时把目录里的每个图片或 .txt 当作一个样本</div>
        </div>
        <MockBadge show={mock} what="结果与评测" />
        <button className="btn" type="button" onClick={() => setShowReg(true)}>登记结果集</button>
      </header>

      <div className="row wrap" style={{ gap: 6 }}>
        <span className="lbl">共 {list.length} 个结果集</span>
        <span className="grow" />
        <span className="lbl">勾选两个以上结果集即可对比</span>
      </div>
      <ErrorNote error={sets.error} />

      <section className="card table-box">
        <table className="tbl">
          <thead><tr>
            <th style={{ width: 28 }} />
            <th>结果集</th><th>位置</th><th className="num">样本数</th><th>来源</th>
            {metricDefs.map((m) => <th key={m.name} className="num" title={m.description}>{m.label} <span style={{ color: 'var(--faint)' }}>{m.higher_is_better ? '↑' : '↓'}</span></th>)}
            <th>评测</th><th className="num">操作</th>
          </tr></thead>
          <tbody>
            {list.map((r) => {
              const st = SET_LOOK[setState(r)]
              return (
                <tr key={r.id} className={focused?.id === r.id ? 'sel' : undefined}>
                  <td><input type="checkbox" aria-label={`选择 ${r.name}`} checked={sel.includes(r.id)} onChange={() => toggle(r.id)} style={{ width: 16, height: 16 }} /></td>
                  <td>
                    <div style={{ fontWeight: 500 }}>{r.name}</div>
                    <div className="lbl">{String(r.meta.model ?? '-')} · {String(r.meta.dataset ?? '-')}{r.note ? ` · ${r.note}` : ''}</div>
                  </td>
                  <td><div className="mono" style={{ fontSize: 12 }}>{r.server_name}</div><div className="lbl mono" title={r.path}>{shortPath(r.path)}</div></td>
                  <td className="mono num">{r.sample_count?.toLocaleString() ?? '-'}</td>
                  <td className="lbl">{r.job_id ? <Link to={`/jobs?id=${r.job_id}`}>任务 #{r.job_id}</Link> : '手工登记'}</td>
                  {metricDefs.map((m) => {
                    const v = r.metrics[m.name]
                    const isBest = v !== undefined && v === best[m.name]
                    return <td key={m.name} className="mono num" style={{ fontWeight: isBest ? 600 : 400, color: v === undefined ? 'var(--faint)' : isBest ? 'var(--ink)' : 'var(--ink-2)' }}>{fmtMetric(v)}</td>
                  })}
                  <td><Badge bg={st.bg} fg={st.fg}>{st.name}</Badge></td>
                  <td className="num">
                    <span className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                      <button type="button" className="btn sm" onClick={() => setFocus(r.id)}>浏览</button>
                      <button type="button" className="btn sm" onClick={() => setEvalFor(r)}>评测</button>
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!sets.loading && list.length === 0 && <div className="empty">还没有结果集，点右上角“登记结果集”</div>}
        {metricDefs.length > 0 && list.length > 0 && <div className="lbl" style={{ padding: '10px 12px' }}>指标列中加粗的是当前列表里的最佳值</div>}
      </section>

      {sel.length >= 2 && (
        <div className="row wrap" style={{ gap: 12, padding: '12px 16px', borderRadius: 8, background: 'var(--ink)', color: '#FFFFFF' }}>
          <span style={{ fontWeight: 500 }}>已选 {sel.length} 个结果集</span>
          <span className="grow" style={{ color: '#C3C8CE', fontSize: 13 }}>{selSets.map((r) => r.name).join('、')}</span>
          <button type="button" className="btn sm" onClick={() => setSel([])} style={{ background: 'transparent', color: '#FFFFFF', borderColor: 'var(--muted)' }}>清空</button>
          <button type="button" className="btn pri sm" onClick={() => navigate(`/compare?ids=${sel.join(',')}`)}>对比所选</button>
        </div>
      )}

      {focused && <SampleBrowser key={focused.id} r={focused} evaluators={evs} mock={mock} />}

      <section className="card" style={{ paddingBottom: 6 }}>
        <div className="card-head">
          <h2 className="grow">评测记录</h2>
          <span className="lbl">评测作为任务在结果所在服务器上运行，输出写到结果目录的 eval/&lt;评测 ID&gt;/</span>
        </div>
        {(evals.data ?? []).map((e) => (
          <div key={e.id} className="list-row wrap" style={{ gap: 12, fontSize: 13 }}>
            <Badge bg={EVAL_LOOK[e.status].bg} fg={EVAL_LOOK[e.status].fg}>{EVAL_LOOK[e.status].name}</Badge>
            <span className="ellipsis" style={{ fontWeight: 500, width: 200 }}>{e.result_set_name}</span>
            {e.job_id ? <Link className="mono" to={`/jobs?id=${e.job_id}`} style={{ width: 60 }}>#{e.job_id}</Link> : <span style={{ width: 60 }}>-</span>}
            <span className="mono lbl grow" style={{ minWidth: 0, flexBasis: 240 }}>
              {e.error ?? (e.values
                ? e.metrics.map((k) => `${metricLabel(evs, k)} ${fmtMetric(e.values?.[k])}`).join(' · ')
                : `${e.metrics.map((k) => metricLabel(evs, k)).join(' · ')} · ${e.status === 'pending' ? '等待运行' : '计算中'}`)}
              {e.errors && Object.keys(e.errors).length > 0 && <span style={{ color: '#8A4A06' }}> · {Object.entries(e.errors).map(([k, v]) => `${metricLabel(evs, k)}：${v}`).join('；')}</span>}
              {!!e.num_skipped && <span style={{ color: '#8A4A06' }}> · 跳过 {e.num_skipped} 个样本</span>}
            </span>
            <span className="lbl">{relTime(e.created_at)}</span>
          </div>
        ))}
        {(evals.data ?? []).length === 0 && <div className="list-row lbl">暂无评测</div>}
      </section>

      {evalFor && <EvalDialog r={evalFor} evaluators={evs} onClose={() => setEvalFor(null)}
        onDone={() => { setEvalFor(null); evals.reload(); sets.reload() }} />}
      {showReg && <RegisterDialog onClose={() => setShowReg(false)} onDone={(r) => { setShowReg(false); sets.reload(); setFocus(r.id) }} />}
    </main>
  )
}

/** 长路径只保留结尾部分，完整路径放在 title 里 */
const shortPath = (p: string, max = 44) => (p.length > max ? `…${p.slice(p.length - max + 1)}` : p)

const KNOWN_FIELDS = ['id', 'image', 'ref_image', 'text', 'ref_text', 'metrics']

function SampleBrowser({ r, evaluators, mock }: { r: ResultSet; evaluators: Evaluator[]; mock: boolean }) {
  const size = 12
  const [page, setPage] = useState(0)
  const data = usePoll(() => resultsApi.samples(r.id, page * size, size), [r.id, page], 0)
  const items = data.data?.items ?? []
  const pages = Math.max(1, Math.ceil((data.data?.total ?? 0) / size))
  const meta = Object.entries(r.meta).filter(([k]) => !['model', 'dataset'].includes(k))
  const kind = sampleKind(items)
  const metricKeys = [...new Set(items.flatMap((s) => Object.keys(s.metrics ?? {})))]
  const extraKeys = [...new Set(items.flatMap((s) => Object.keys(s).filter((k) => !KNOWN_FIELDS.includes(k))))]
  const sid = (s: Sample, i: number) => String(s.id ?? page * size + i)
  const hasImage = items.some((s) => typeof s.image === 'string')

  return (
    <section className="card col">
      <div className="row wrap" style={{ padding: '16px 18px', gap: 12, borderBottom: '1px solid var(--border-soft)' }}>
        <div className="grow">
          <h2>样本浏览 · {r.name}</h2>
          <div className="lbl" style={{ marginTop: 4 }}>
            meta.json：模型 {String(r.meta.model ?? '-')} · 数据集 {String(r.meta.dataset ?? '-')}{meta.map(([k, v]) => ` · ${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join('')}
          </div>
        </div>
        <span className="row" style={{ gap: 6 }}>
          <button type="button" className="btn sm" aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
          <span className="mono lbl">{page + 1} / {pages}</span>
          <button type="button" className="btn sm" aria-label="下一页" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>›</button>
        </span>
      </div>
      <ErrorNote error={data.error} />
      {kind === 'image' ? (
        <div style={{ padding: '16px 18px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 14 }}>
          {items.map((s, i) => (
            <figure key={sid(s, i)} className="col" style={{ margin: 0, gap: 6 }}>
              {s.image ? <SampleImage src={resultsApi.fileUrl(r.id, s.image)} seed={sid(s, i)} mock={mock} label={`样本 ${sid(s, i)}`} /> : <div className="thumb" />}
              <figcaption className="row" style={{ justifyContent: 'space-between', fontSize: 12 }}>
                <span className="mono">{sid(s, i)}</span>
                <span className="mono lbl">{metricKeys.slice(0, 1).map((k) => `${metricLabel(evaluators, k)} ${fmtMetric(s.metrics?.[k])}`).join('')}</span>
              </figcaption>
            </figure>
          ))}
        </div>
      ) : (
        <div className="table-box">
          <table className="tbl">
            <thead><tr>
              <th>id</th>
              {kind === 'text' && <>{hasImage && <th>图片</th>}<th>识别结果</th><th>参考答案</th></>}
              {extraKeys.map((k) => <th key={k}>{k}</th>)}
              {metricKeys.map((k) => <th key={k} className="num">{metricLabel(evaluators, k)}</th>)}
            </tr></thead>
            <tbody>
              {items.map((s, i) => (
                <tr key={sid(s, i)}>
                  <td className="mono">{sid(s, i)}</td>
                  {kind === 'text' && (
                    <>
                      {hasImage && <td style={{ width: 140 }}>{s.image ? <SampleImage src={resultsApi.fileUrl(r.id, s.image)} seed={sid(s, i)} mock={mock} label={`样本 ${sid(s, i)}`} style={{ width: 120, aspectRatio: '4 / 1' }} /> : '-'}</td>}
                      <td style={{ whiteSpace: 'normal' }}>{s.text ?? '-'}</td>
                      <td style={{ whiteSpace: 'normal', color: 'var(--muted)' }}>{s.ref_text ?? '-'}</td>
                    </>
                  )}
                  {extraKeys.map((k) => <td key={k} className="lbl" style={{ whiteSpace: 'normal' }}>{s[k] === undefined ? '-' : typeof s[k] === 'object' ? JSON.stringify(s[k]) : String(s[k])}</td>)}
                  {metricKeys.map((k) => {
                    const v = s.metrics?.[k]
                    const bad = v !== undefined && ((k === 'cer' && v > 0) || (k === 'ocr_a' && v < 1))
                    return <td key={k} className="mono num" style={{ color: bad ? '#8E2219' : 'var(--ink-2)' }}>{fmtMetric(v)}</td>
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!data.loading && items.length === 0 && <div className="empty">没有样本</div>}
      {items.length > 0 && metricKeys.length === 0 && <div className="lbl" style={{ padding: '10px 18px' }}>还没有逐样本指标，评测完成后显示</div>}
    </section>
  )
}

function EvalDialog({ r, evaluators, onClose, onDone }: { r: ResultSet; evaluators: Evaluator[]; onClose: () => void; onDone: () => void }) {
  const [picked, setPicked] = useState<string[] | null>(null)
  const [reference, setReference] = useState('')
  const [devicesInput, setDevicesInput] = useState<string | null>(null)
  const [priority, setPriority] = useState('0')
  const [submitter, setSubmitter] = useState(() => localStorage.getItem('gnm.submitter') ?? '')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 读第一条样本判断是图片还是文字结果，默认勾选对应的指标
  const probe = usePoll(() => resultsApi.samples(r.id, 0, 1), [r.id], 0)
  const kind = probe.data ? sampleKind(probe.data.items) : null
  const chosen = picked ?? (kind ? evaluators.filter((m) => m.kind === kind).map((m) => m.name) : [])
  // 没手动填卡数时，选了 LPIPS 默认用 1 张卡
  const devices = devicesInput ?? (chosen.includes('lpips') ? '1' : '0')
  const toggle = (name: string) => setPicked(chosen.includes(name) ? chosen.filter((x) => x !== name) : [...chosen, name])

  const submit = async () => {
    setErr(null)
    setBusy(true)
    try {
      localStorage.setItem('gnm.submitter', submitter)
      await resultsApi.evaluate({
        result_set_id: r.id, metrics: chosen, reference: reference.trim() || null,
        num_devices: Number(devices) || 0, priority: Math.max(-100, Math.min(100, Number(priority) || 0)), submitter: submitter.trim() || null,
      })
      onDone()
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title="发起评测" onClose={onClose} width={560}>
      <div className="lbl" style={{ marginTop: -8 }}>结果集：<span className="mono">{r.name}</span> · {r.server_name}:{r.path}</div>
      <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        {(['image', 'text'] as const).map((k) => (
          <div key={k} className="field">
            <span className="lbl">{k === 'image' ? '图像指标（需要 image、ref_image 字段）' : '文字指标（需要 text、ref_text 字段）'}</span>
            <div className="row wrap" style={{ gap: 10, alignItems: 'stretch' }}>
              {evaluators.filter((m) => m.kind === k).map((m) => (
                <button key={m.name} type="button" className={chosen.includes(m.name) ? 'ev on' : 'ev'} onClick={() => toggle(m.name)} aria-pressed={chosen.includes(m.name)} title={m.description}>
                  <span style={{ fontWeight: 500 }}>{m.label}</span>
                  <span className="lbl">{m.higher_is_better ? '越高越好' : '越低越好'}{m.unit ? ` · ${m.unit}` : ''}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
        <label className="field"><span className="lbl">参考值（可选，服务器上的参考目录，按文件名配对图片和 .txt；也可以是 jsonl，按 id 合并 ref_image / ref_text）</span>
          <input className="inp mono" style={{ fontSize: 13 }} placeholder="/data/gt，predictions.jsonl 已带参考值时留空" value={reference} onChange={(e) => setReference(e.target.value)} />
        </label>
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">卡数（0 ~ 8）</span><input className="inp mono" inputMode="numeric" value={devices} onChange={(e) => setDevicesInput(e.target.value)} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">优先级（-100 ~ 100）</span><input className="inp mono" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">提交人</span><input className="inp" value={submitter} onChange={(e) => setSubmitter(e.target.value)} /></label>
        </div>
        <div className="notice">提交后作为任务进入队列，在结果集所在服务器上运行。LPIPS 可以用 1 张卡加速，其他指标用 0 张卡即可。</div>
        {err && <div className="notice err">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="submit" className="btn pri" disabled={busy || chosen.length === 0}>{busy ? '提交中' : '提交评测任务'}</button>
        </div>
      </form>
    </Modal>
  )
}

function RegisterDialog({ onClose, onDone }: { onClose: () => void; onDone: (r: ResultSet) => void }) {
  const servers = usePoll(() => serversApi.list({ include_devices: false }), [], 0)
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [serverId, setServerId] = useState('')
  const [path, setPath] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const sid = serverId || String(servers.data?.[0]?.id ?? '')
  const submit = async () => {
    setErr(null)
    try {
      onDone(await resultsApi.create({ server_id: Number(sid), path: path.trim(), name: name.trim() || null, note: note.trim() || null }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    }
  }
  return (
    <Modal title="登记结果集" onClose={onClose}>
      <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">所在服务器</span>
            <select className="inp" value={sid} onChange={(e) => setServerId(e.target.value)}>
              {(servers.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <label className="field" style={{ flex: '2 1 0' }}><span className="lbl">目录路径（绝对路径）</span><input className="inp mono" style={{ fontSize: 13 }} placeholder="/data/results/..." value={path} onChange={(e) => setPath(e.target.value)} required autoFocus /></label>
        </div>
        <label className="field"><span className="lbl">名称（可选，默认取 meta.json 的 name 或目录名）</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span className="lbl">备注（可选）</span><input className="inp" value={note} onChange={(e) => setNote(e.target.value)} /></label>
        <div className="notice">登记时读取目录下的 meta.json（可选）并统计样本数：有 predictions.jsonl 时按它统计，没有时统计目录里的图片和 .txt（同名的算一个样本）。文件留在原服务器上。</div>
        {err && <div className="notice err">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="submit" className="btn pri" disabled={!sid}>登记</button>
        </div>
      </form>
    </Modal>
  )
}
