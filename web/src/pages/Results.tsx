import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { resultsApi } from '../api/results'
import { serversApi } from '../api/servers'
import type { EvalConfig, EvalConfigBody, Evaluation, Evaluator, MetricKind, Project, ResultSet, Sample } from '../api/types'
import { Badge, ErrorNote, MockBadge, Modal, SampleImage, Switch } from '../components/common'
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

/** 样本的识别文本：样本自带的 text，没有时取评测 OCR 出的文本 */
export const sampleText = (s: Sample | null | undefined) => (typeof s?.text === 'string' ? s.text : typeof s?.ocr_text === 'string' ? s.ocr_text : undefined)

/** 运行中评测的进度，例如“LQ 基线 已处理 120/500（24%），预计还要 3 分钟” */
function progressText(e: Evaluation): string {
  const p = e.progress
  if (!p || p.total <= 0) return '计算中'
  const stage = p.stage === 'lq' ? 'LQ 基线 ' : ''
  let text = `${stage}已处理 ${p.done}/${p.total}（${Math.floor((100 * p.done) / p.total)}%）`
  if (p.done > 0 && p.done < p.total) {
    const left = Math.round(((p.total - p.done) * p.elapsed) / p.done / 60)
    text += left < 1 ? '，预计不到 1 分钟' : `，预计还要 ${left} 分钟`
  }
  return text
}

/** 标签输入：逗号、顿号或空白分隔 */
const parseTags = (text: string) => [...new Set(text.split(/[,，、\s]+/).map((t) => t.trim()).filter(Boolean))]

/** 样本的展示方式：有识别文本按文字展示，否则有 image 字段按图片展示 */
export function sampleKind(items: (Sample | null | undefined)[]): MetricKind | null {
  if (items.some((s) => sampleText(s) !== undefined)) return 'text'
  if (items.some((s) => typeof s?.image === 'string')) return 'image'
  return null
}

export default function ResultsPage() {
  const navigate = useNavigate()
  const [sel, setSel] = useState<number[]>([])
  const [focus, setFocus] = useState<number | null>(null)
  const [evalFor, setEvalFor] = useState<ResultSet | null>(null)
  const [showReg, setShowReg] = useState(false)
  const [editFor, setEditFor] = useState<ResultSet | null>(null)
  const [showProjects, setShowProjects] = useState(false)
  const [showConfigs, setShowConfigs] = useState(false)
  // 筛选：项目（'' 全部，'0' 未归档）、标签（需全部包含）、名称关键字
  const [project, setProject] = useState('')
  const [tagSel, setTagSel] = useState<string[]>([])
  const [q, setQ] = useState('')
  const filters = { project_id: project === '' ? undefined : Number(project), tag: tagSel, q: q.trim() || undefined }
  const sets = usePoll(() => resultsApi.list(filters), [project, tagSel.join('\n'), q.trim()], 15_000)
  const projects = usePoll(() => resultsApi.projects(), [], 0)
  const tags = usePoll(() => resultsApi.tags(), [], 30_000)
  const evaluators = usePoll(() => resultsApi.evaluators(), [], 0)
  const evals = usePoll(() => resultsApi.evaluations(), [], 10_000)
  const mock = useMock('results')
  const reloadAll = () => { sets.reload(); projects.reload(); tags.reload() }

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
        <button className="btn" type="button" onClick={() => setShowProjects(true)}>项目</button>
        <button className="btn" type="button" onClick={() => setShowConfigs(true)}>评测配置</button>
        <button className="btn pri" type="button" onClick={() => setShowReg(true)}>登记结果集</button>
      </header>

      <div className="row wrap" style={{ gap: 10 }}>
        <select className="inp" aria-label="按项目筛选" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">全部项目</option>
          <option value="0">未归档</option>
          {(projects.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}（{p.result_count}）</option>)}
        </select>
        <input className="inp" aria-label="按名称搜索" placeholder="搜索名称、路径、备注" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} />
        {(tags.data ?? []).map((t) => {
          const on = tagSel.includes(t.tag)
          return (
            <button key={t.tag} type="button" className={on ? 'chip on' : 'chip'} aria-pressed={on}
              onClick={() => setTagSel(on ? tagSel.filter((x) => x !== t.tag) : [...tagSel, t.tag])}>
              {t.tag} <span className="lbl">{t.count}</span>
            </button>
          )
        })}
        {(project !== '' || tagSel.length > 0 || q) && <button type="button" className="btn sm" onClick={() => { setProject(''); setTagSel([]); setQ('') }}>清除筛选</button>}
        <span className="grow" />
        <span className="lbl">共 {list.length} 个结果集 · 勾选两个以上即可对比</span>
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
                    {(r.project_name || r.tags.length > 0) && (
                      <div className="row wrap" style={{ gap: 4, marginTop: 4 }}>
                        {r.project_name && <Badge bg="#DCE7FC" fg="#123F99">{r.project_name}</Badge>}
                        {r.tags.map((t) => <Badge key={t} bg="#ECEDEB" fg="#3A4048">{t}</Badge>)}
                      </div>
                    )}
                  </td>
                  <td><div className="mono" style={{ fontSize: 12 }}>{r.server_name}</div><div className="lbl mono" title={r.path}>{shortPath(r.path)}</div></td>
                  <td className="mono num">{r.sample_count?.toLocaleString() ?? '-'}</td>
                  <td className="lbl">{r.job_id ? <Link to={`/jobs?id=${r.job_id}`}>任务 #{r.job_id}</Link> : '手工登记'}</td>
                  {metricDefs.map((m) => {
                    const v = r.metrics[m.name]
                    const isBest = v !== undefined && v === best[m.name]
                    const lq = r.lq_metrics?.[m.name]
                    return (
                      <td key={m.name} className="mono num" style={{ fontWeight: isBest ? 600 : 400, color: v === undefined ? 'var(--faint)' : isBest ? 'var(--ink)' : 'var(--ink-2)' }}>
                        {fmtMetric(v)}
                        {lq !== undefined && <div className="lbl" style={{ fontWeight: 400 }} title="LQ 基线（LQ 图片与同样的参考值计算）">LQ {fmtMetric(lq)}</div>}
                      </td>
                    )
                  })}
                  <td><Badge bg={st.bg} fg={st.fg}>{st.name}</Badge></td>
                  <td className="num">
                    <span className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                      <button type="button" className="btn sm" onClick={() => setFocus(r.id)}>浏览</button>
                      <button type="button" className="btn sm" onClick={() => setEditFor(r)}>编辑</button>
                      <button type="button" className="btn sm" onClick={() => setEvalFor(r)}>评测</button>
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!sets.loading && list.length === 0 && <div className="empty">{project !== '' || tagSel.length > 0 || q ? '没有符合筛选条件的结果集' : '还没有结果集，点右上角“登记结果集”'}</div>}
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
          <span className="lbl">评测作为任务在评测服务器上运行（未指定时在结果所在服务器上），输出写到该服务器上结果目录的 eval/&lt;评测 ID&gt;/</span>
        </div>
        {(evals.data ?? []).map((e) => (
          <div key={e.id} className="list-row wrap" style={{ gap: 12, fontSize: 13 }}>
            <Badge bg={EVAL_LOOK[e.status].bg} fg={EVAL_LOOK[e.status].fg}>{EVAL_LOOK[e.status].name}</Badge>
            <span className="col" style={{ width: 200, minWidth: 0 }}>
              <span className="ellipsis" style={{ fontWeight: 500 }}>{e.result_set_name}</span>
              <span className="lbl ellipsis" title={e.data_path}>{e.config_name ? `${e.config_name} · ` : ''}{e.server_name}</span>
            </span>
            {e.job_id ? <Link className="mono" to={`/jobs?id=${e.job_id}`} style={{ width: 60 }}>#{e.job_id}</Link> : <span style={{ width: 60 }}>-</span>}
            <span className="mono lbl grow" style={{ minWidth: 0, flexBasis: 240 }}>
              {e.error ?? (e.values
                ? e.metrics.map((k) => `${metricLabel(evs, k)} ${fmtMetric(e.values?.[k])}`).join(' · ')
                : `${e.metrics.map((k) => metricLabel(evs, k)).join(' · ')} · ${e.status === 'copying' ? `正在拷贝到 ${e.server_name}:${e.data_path}` : e.status === 'pending' ? '等待运行' : progressText(e)}`)}
              {e.lq_values && <span> · LQ 基线{e.compute_lq ? '' : '（复用）'} {Object.entries(e.lq_values).map(([k, v]) => `${metricLabel(evs, k)} ${fmtMetric(v)}`).join(' · ')}</span>}
              {!e.lq_values && e.lq_dir && e.status !== 'failed' && <span> · {e.compute_lq ? '本次同时计算 LQ 基线' : 'LQ 基线复用同配置的第一次评测'}</span>}
              {e.errors && Object.keys(e.errors).length > 0 && <span style={{ color: '#8A4A06' }}> · {Object.entries(e.errors).map(([k, v]) => `${metricLabel(evs, k)}：${v}`).join('；')}</span>}
              {!!e.num_skipped && <span style={{ color: '#8A4A06' }}> · 跳过 {e.num_skipped} 个样本</span>}
            </span>
            {e.status === 'running' && e.progress && e.progress.total > 0 && (
              <div className="bar" style={{ width: 120 }} title={progressText(e)}>
                <div style={{ width: `${(100 * e.progress.done) / e.progress.total}%`, background: 'var(--accent)' }} />
              </div>
            )}
            <span className="lbl">{relTime(e.created_at)}</span>
          </div>
        ))}
        {(evals.data ?? []).length === 0 && <div className="list-row lbl">暂无评测</div>}
      </section>

      {evalFor && <EvalDialog r={evalFor} evaluators={evs} onClose={() => setEvalFor(null)}
        onDone={() => { setEvalFor(null); evals.reload(); sets.reload() }} />}
      {showReg && <RegisterDialog projects={projects.data ?? []} onClose={() => setShowReg(false)} onDone={(r) => { setShowReg(false); reloadAll(); setFocus(r.id) }} />}
      {editFor && <EditDialog r={editFor} projects={projects.data ?? []} onClose={() => setEditFor(null)} onDone={() => { setEditFor(null); reloadAll() }} />}
      {showProjects && <ProjectsDialog onClose={() => { setShowProjects(false); reloadAll() }} />}
      {showConfigs && <ConfigsDialog evaluators={evs} onClose={() => setShowConfigs(false)} />}
    </main>
  )
}

/** 长路径只保留结尾部分，完整路径放在 title 里 */
const shortPath = (p: string, max = 44) => (p.length > max ? `…${p.slice(p.length - max + 1)}` : p)

const KNOWN_FIELDS = ['id', 'image', 'ref_image', 'text', 'ref_text', 'metrics', 'lq_image', 'ocr_text', 'media']

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
  // 评测配对到 LQ / GT 图片时可以和输出并排展示
  const hasLq = items.some((s) => typeof s.lq_image === 'string')
  const hasRef = items.some((s) => typeof s.ref_image === 'string')
  const [showLq, setShowLq] = useState(true)
  const [showRef, setShowRef] = useState(false)
  const panels = (s: Sample) => [
    ...(hasLq && showLq ? [['lq_image', 'LQ'] as const] : []),
    ['image', '输出'] as const,
    ...(hasRef && showRef ? [['ref_image', 'GT'] as const] : []),
  ].filter(([f]) => typeof s[f] === 'string')

  return (
    <section className="card col">
      <div className="row wrap" style={{ padding: '16px 18px', gap: 12, borderBottom: '1px solid var(--border-soft)' }}>
        <div className="grow">
          <h2>样本浏览 · {r.name}</h2>
          <div className="lbl" style={{ marginTop: 4 }}>
            meta.json：模型 {String(r.meta.model ?? '-')} · 数据集 {String(r.meta.dataset ?? '-')}{meta.map(([k, v]) => ` · ${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join('')}
          </div>
        </div>
        {hasLq && <span className="row" style={{ gap: 6 }}><span className="lbl">LQ</span><Switch on={showLq} onChange={setShowLq} label="并排显示 LQ" /></span>}
        {hasRef && <span className="row" style={{ gap: 6 }}><span className="lbl">GT</span><Switch on={showRef} onChange={setShowRef} label="并排显示 GT" /></span>}
        <span className="row" style={{ gap: 6 }}>
          <button type="button" className="btn sm" aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
          <span className="mono lbl">{page + 1} / {pages}</span>
          <button type="button" className="btn sm" aria-label="下一页" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>›</button>
        </span>
      </div>
      <ErrorNote error={data.error} />
      {kind === 'image' ? (
        <div style={{ padding: '16px 18px', display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${(hasLq && showLq ? 1 : 0) + (hasRef && showRef ? 1 : 0) ? 360 : 180}px, 1fr))`, gap: 14 }}>
          {items.map((s, i) => (
            <figure key={sid(s, i)} className="col" style={{ margin: 0, gap: 6 }}>
              {s.image ? (
                <div className="row" style={{ gap: 6, alignItems: 'stretch' }}>
                  {panels(s).map(([f, label]) => (
                    <div key={f} className="col" style={{ flex: '1 1 0', minWidth: 0, gap: 2 }}>
                      <SampleImage src={resultsApi.sampleFileUrl(r.id, s, f)} seed={sid(s, i)} mock={mock} blur={f === 'lq_image' ? 2 : 0} label={`${label} ${sid(s, i)}`} />
                      {panels(s).length > 1 && <span className="lbl" style={{ fontSize: 11 }}>{label}</span>}
                    </div>
                  ))}
                </div>
              ) : <div className="thumb" />}
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
              {kind === 'text' && <>{hasLq && showLq && <th>LQ</th>}{hasImage && <th>图片</th>}<th>识别结果</th><th>参考答案</th></>}
              {extraKeys.map((k) => <th key={k}>{k}</th>)}
              {metricKeys.map((k) => <th key={k} className="num">{metricLabel(evaluators, k)}</th>)}
            </tr></thead>
            <tbody>
              {items.map((s, i) => (
                <tr key={sid(s, i)}>
                  <td className="mono">{sid(s, i)}</td>
                  {kind === 'text' && (
                    <>
                      {hasLq && showLq && <td style={{ width: 140 }}>{s.lq_image ? <SampleImage src={resultsApi.sampleFileUrl(r.id, s, 'lq_image')} seed={sid(s, i)} mock={mock} blur={2} label={`LQ ${sid(s, i)}`} style={{ width: 120, aspectRatio: '4 / 1' }} /> : '-'}</td>}
                      {hasImage && <td style={{ width: 140 }}>{s.image ? <SampleImage src={resultsApi.fileUrl(r.id, s.image)} seed={sid(s, i)} mock={mock} label={`样本 ${sid(s, i)}`} style={{ width: 120, aspectRatio: '4 / 1' }} /> : '-'}</td>}
                      <td style={{ whiteSpace: 'normal' }}>{sampleText(s) ?? '-'}{s.text === undefined && s.ocr_text !== undefined && <span className="lbl">（OCR）</span>}</td>
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

/** 指标多选按钮，按图像、文字分组 */
function MetricPicker({ evaluators, chosen, onToggle }: { evaluators: Evaluator[]; chosen: string[]; onToggle: (name: string) => void }) {
  return (
    <>
      {(['image', 'text'] as const).map((k) => (
        <div key={k} className="field">
          <span className="lbl">{k === 'image' ? '图像指标（需要输出图片和 GT 图片）' : '文字指标（需要识别文本或可 OCR 的图片，以及文字标注）'}</span>
          <div className="row wrap" style={{ gap: 10, alignItems: 'stretch' }}>
            {evaluators.filter((m) => m.kind === k).map((m) => (
              <button key={m.name} type="button" className={chosen.includes(m.name) ? 'ev on' : 'ev'} onClick={() => onToggle(m.name)} aria-pressed={chosen.includes(m.name)} title={m.description}>
                <span style={{ fontWeight: 500 }}>{m.label}</span>
                <span className="lbl">{m.higher_is_better ? '越高越好' : '越低越好'}{m.unit ? ` · ${m.unit}` : ''}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </>
  )
}

const errText = (e: unknown) => (e instanceof ApiError ? e.detail : String(e))

function EvalDialog({ r, evaluators, onClose, onDone }: { r: ResultSet; evaluators: Evaluator[]; onClose: () => void; onDone: () => void }) {
  const configs = usePoll(() => resultsApi.configs(), [], 0)
  const [configId, setConfigId] = useState('')
  const [picked, setPicked] = useState<string[] | null>(null)
  const [reference, setReference] = useState('')
  const [devicesInput, setDevicesInput] = useState<string | null>(null)
  const [priority, setPriority] = useState('0')
  const [submitter, setSubmitter] = useState(() => localStorage.getItem('gnm.submitter') ?? '')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const config = (configs.data ?? []).find((c) => String(c.id) === configId)
  // 读第一条样本判断是图片还是文字结果，默认勾选对应的指标
  const probe = usePoll(() => resultsApi.samples(r.id, 0, 1), [r.id], 0)
  const kind = probe.data ? sampleKind(probe.data.items) : null
  const chosen = picked ?? config?.metrics ?? (kind ? evaluators.filter((m) => m.kind === kind).map((m) => m.name) : [])
  // 没手动填卡数时取配置中的卡数；选了 LPIPS 默认用 1 张卡
  const devices = devicesInput ?? String(config ? config.num_devices : chosen.includes('lpips') ? 1 : 0)
  const toggle = (name: string) => setPicked(chosen.includes(name) ? chosen.filter((x) => x !== name) : [...chosen, name])
  const pickConfig = (id: string) => { setConfigId(id); setPicked(null); setDevicesInput(null) }

  const submit = async () => {
    setErr(null)
    setBusy(true)
    try {
      localStorage.setItem('gnm.submitter', submitter)
      await resultsApi.evaluate({
        result_set_id: r.id, config_id: config?.id ?? null, metrics: chosen, reference: reference.trim() || null,
        num_devices: Number(devices) || 0, priority: Math.max(-100, Math.min(100, Number(priority) || 0)), submitter: submitter.trim() || null,
      })
      onDone()
    } catch (e) {
      setErr(errText(e))
    } finally {
      setBusy(false)
    }
  }
  const copies = config?.server_id && config.server_id !== r.server_id
  return (
    <Modal title="发起评测" onClose={onClose} width={600}>
      <div className="lbl" style={{ marginTop: -8 }}>结果集：<span className="mono">{r.name}</span> · {r.server_name}:{r.path}</div>
      <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        <label className="field"><span className="lbl">评测配置（在右上角“评测配置”里保存和修改）</span>
          <select className="inp" value={configId} onChange={(e) => pickConfig(e.target.value)}>
            <option value="">不使用配置，手动填写</option>
            {(configs.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        {config && (
          <div className="notice col" style={{ gap: 2 }}>
            <span>Label 文件：<span className="mono">{config.label_file ?? '-'}</span></span>
            <span>GT 目录：<span className="mono">{config.gt_dir ?? '-'}</span></span>
            <span>LQ 目录：<span className="mono">{config.lq_dir ?? '-'}</span></span>
            <span>评测服务器：{config.server_name ? <span className="mono">{config.server_name}:{config.server_path}</span> : '结果所在服务器'}</span>
            {copies && <span>提交后先把结果目录拷贝到 <span className="mono">{config.server_path}/result_{r.id}</span>，再在 {config.server_name} 上评测。</span>}
            {config.lq_dir && <span>LQ 基线指标每个配置只在第一次评测时计算，之后的评测直接复用。</span>}
          </div>
        )}
        <MetricPicker evaluators={evaluators} chosen={chosen} onToggle={toggle} />
        {!config && (
          <label className="field"><span className="lbl">参考值（可选，服务器上的参考目录，按文件名配对图片和 .txt；也可以是 jsonl，或 PaddleOCR 格式的文字标注文件 Label.txt）</span>
            <input className="inp mono" style={{ fontSize: 13 }} placeholder="/data/gt，predictions.jsonl 已带参考值时留空" value={reference} onChange={(e) => setReference(e.target.value)} />
          </label>
        )}
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">卡数（0 ~ 8）</span><input className="inp mono" inputMode="numeric" value={devices} onChange={(e) => setDevicesInput(e.target.value)} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">优先级（-100 ~ 100）</span><input className="inp mono" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">提交人</span><input className="inp" value={submitter} onChange={(e) => setSubmitter(e.target.value)} /></label>
        </div>
        <div className="notice">提交后作为任务进入队列。LPIPS 和需要 OCR 的文字指标（样本只有图片、没有识别文本时会用 PaddleOCR 识别）可以用 1 张卡加速，其他指标用 0 张卡即可。</div>
        {err && <div className="notice err">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="submit" className="btn pri" disabled={busy || chosen.length === 0}>{busy ? '提交中' : '提交评测任务'}</button>
        </div>
      </form>
    </Modal>
  )
}

/** 项目选择，'' 表示不归档 */
function ProjectSelect({ projects, value, onChange }: { projects: Project[]; value: string; onChange: (v: string) => void }) {
  return (
    <select className="inp" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">不归档到项目</option>
      {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
    </select>
  )
}

function RegisterDialog({ projects, onClose, onDone }: { projects: Project[]; onClose: () => void; onDone: (r: ResultSet) => void }) {
  const servers = usePoll(() => serversApi.list({ include_devices: false }), [], 0)
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const [serverId, setServerId] = useState('')
  const [path, setPath] = useState('')
  const [project, setProject] = useState('')
  const [tagText, setTagText] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const sid = serverId || String(servers.data?.[0]?.id ?? '')
  const submit = async () => {
    setErr(null)
    try {
      onDone(await resultsApi.create({
        server_id: Number(sid), path: path.trim(), name: name.trim() || null, note: note.trim() || null,
        project_id: project ? Number(project) : null, tags: parseTags(tagText),
      }))
    } catch (e) {
      setErr(errText(e))
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
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">项目</span><ProjectSelect projects={projects} value={project} onChange={setProject} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">标签（逗号或空格分隔）</span><input className="inp" value={tagText} onChange={(e) => setTagText(e.target.value)} /></label>
        </div>
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

function EditDialog({ r, projects, onClose, onDone }: { r: ResultSet; projects: Project[]; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(r.name)
  const [note, setNote] = useState(r.note ?? '')
  const [project, setProject] = useState(r.project_id ? String(r.project_id) : '')
  const [tagText, setTagText] = useState(r.tags.join(', '))
  const [err, setErr] = useState<string | null>(null)
  const submit = async () => {
    setErr(null)
    try {
      await resultsApi.update(r.id, { name: name.trim(), note: note.trim() || null, project_id: project ? Number(project) : null, tags: parseTags(tagText) })
      onDone()
    } catch (e) {
      setErr(errText(e))
    }
  }
  return (
    <Modal title={`编辑结果集 · ${r.name}`} onClose={onClose}>
      <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        <label className="field"><span className="lbl">名称</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} required /></label>
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">项目</span><ProjectSelect projects={projects} value={project} onChange={setProject} /></label>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">标签（逗号或空格分隔）</span><input className="inp" value={tagText} onChange={(e) => setTagText(e.target.value)} /></label>
        </div>
        <label className="field"><span className="lbl">备注</span><input className="inp" value={note} onChange={(e) => setNote(e.target.value)} /></label>
        {err && <div className="notice err">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="submit" className="btn pri" disabled={!name.trim()}>保存</button>
        </div>
      </form>
    </Modal>
  )
}

function ProjectsDialog({ onClose }: { onClose: () => void }) {
  const projects = usePoll(() => resultsApi.projects(), [], 0)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [editing, setEditing] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const run = async (fn: () => Promise<unknown>) => {
    setErr(null)
    try {
      await fn()
      projects.reload()
      return true
    } catch (e) {
      setErr(errText(e))
      return false
    }
  }
  const save = async () => {
    const body = { name: name.trim(), description: description.trim() || null }
    if (await run(() => (editing ? resultsApi.updateProject(editing, body) : resultsApi.createProject(body)))) {
      setName(''); setDescription(''); setEditing(null)
    }
  }
  const remove = (p: Project) => {
    if (window.confirm(`删除项目“${p.name}”？其中的 ${p.result_count} 个结果集会改为未归档，不会被删除。`)) run(() => resultsApi.deleteProject(p.id))
  }
  return (
    <Modal title="项目" onClose={onClose} width={560}>
      <div className="col" style={{ gap: 0 }}>
        {(projects.data ?? []).map((p) => (
          <div key={p.id} className="list-row" style={{ gap: 12 }}>
            <span className="col grow" style={{ minWidth: 0 }}>
              <span style={{ fontWeight: 500 }}>{p.name}</span>
              <span className="lbl">{p.result_count} 个结果集{p.description ? ` · ${p.description}` : ''}</span>
            </span>
            <button type="button" className="btn sm" onClick={() => { setEditing(p.id); setName(p.name); setDescription(p.description ?? '') }}>修改</button>
            <button type="button" className="btn sm" onClick={() => remove(p)}>删除</button>
          </div>
        ))}
        {!projects.loading && (projects.data ?? []).length === 0 && <div className="list-row lbl">还没有项目</div>}
      </div>
      <form className="col" style={{ gap: 12 }} onSubmit={(e) => { e.preventDefault(); save() }}>
        <div className="row" style={{ gap: 12 }}>
          <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">{editing ? '项目名称' : '新项目名称'}</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} required /></label>
          <label className="field" style={{ flex: '2 1 0', minWidth: 0 }}><span className="lbl">说明（可选）</span><input className="inp" value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        </div>
        {err && <div className="notice err">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          {editing && <button type="button" className="btn" onClick={() => { setEditing(null); setName(''); setDescription('') }}>取消修改</button>}
          <button type="submit" className="btn pri" disabled={!name.trim()}>{editing ? '保存' : '创建项目'}</button>
        </div>
      </form>
    </Modal>
  )
}

const EMPTY_CONFIG: EvalConfigBody = { name: '', metrics: [], label_file: null, gt_dir: null, lq_dir: null, server_id: null, server_path: null, num_devices: 0, note: null }

function ConfigsDialog({ evaluators, onClose }: { evaluators: Evaluator[]; onClose: () => void }) {
  const configs = usePoll(() => resultsApi.configs(), [], 0)
  const servers = usePoll(() => serversApi.list({ include_devices: false }), [], 0)
  const [form, setForm] = useState<EvalConfigBody | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const set = (patch: Partial<EvalConfigBody>) => setForm((f) => (f ? { ...f, ...patch } : f))
  const open = (c: EvalConfig | null) => {
    setErr(null)
    setEditing(c?.id ?? null)
    setForm(c ? { name: c.name, metrics: c.metrics, label_file: c.label_file, gt_dir: c.gt_dir, lq_dir: c.lq_dir, server_id: c.server_id, server_path: c.server_path, num_devices: c.num_devices, note: c.note } : { ...EMPTY_CONFIG })
  }
  const save = async () => {
    if (!form) return
    setErr(null)
    const trim = (v: string | null) => v?.trim() || null
    const body = { ...form, name: form.name.trim(), label_file: trim(form.label_file), gt_dir: trim(form.gt_dir), lq_dir: trim(form.lq_dir), server_path: form.server_id ? trim(form.server_path) : null, note: trim(form.note) }
    try {
      await (editing ? resultsApi.updateConfig(editing, body) : resultsApi.createConfig(body))
      setForm(null)
      configs.reload()
    } catch (e) {
      setErr(errText(e))
    }
  }
  const remove = async (c: EvalConfig) => {
    if (!window.confirm(`删除评测配置“${c.name}”？已有的评测记录不受影响。`)) return
    try {
      await resultsApi.deleteConfig(c.id)
      configs.reload()
    } catch (e) {
      setErr(errText(e))
    }
  }
  const pathInput = (key: 'label_file' | 'gt_dir' | 'lq_dir' | 'server_path', label: string, placeholder: string) => (
    <label className="field"><span className="lbl">{label}</span>
      <input className="inp mono" style={{ fontSize: 13 }} placeholder={placeholder} value={form?.[key] ?? ''} onChange={(e) => set({ [key]: e.target.value })} />
    </label>
  )
  return (
    <Modal title="评测配置" onClose={onClose} width={680}>
      {!form && (
        <>
          <div className="col" style={{ gap: 0 }}>
            {(configs.data ?? []).map((c) => (
              <div key={c.id} className="list-row" style={{ gap: 12, alignItems: 'flex-start' }}>
                <span className="col grow" style={{ minWidth: 0, gap: 2 }}>
                  <span style={{ fontWeight: 500 }}>{c.name}</span>
                  <span className="lbl">{c.metrics.map((k) => metricLabel(evaluators, k)).join(' · ')} · {c.server_name ? `在 ${c.server_name}:${c.server_path} 评测` : '在结果所在服务器上评测'}</span>
                  {[['Label', c.label_file], ['GT', c.gt_dir], ['LQ', c.lq_dir]].filter(([, v]) => v).map(([k, v]) => (
                    <span key={k} className="lbl mono ellipsis" title={v ?? ''}>{k} {v}</span>
                  ))}
                </span>
                <button type="button" className="btn sm" onClick={() => open(c)}>修改</button>
                <button type="button" className="btn sm" onClick={() => remove(c)}>删除</button>
              </div>
            ))}
            {!configs.loading && (configs.data ?? []).length === 0 && <div className="list-row lbl">还没有评测配置</div>}
          </div>
          {err && <div className="notice err">{err}</div>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn pri" onClick={() => open(null)}>新建配置</button>
          </div>
        </>
      )}
      {form && (
        <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); save() }}>
          <label className="field"><span className="lbl">配置名称</span><input className="inp" value={form.name} onChange={(e) => set({ name: e.target.value })} required autoFocus /></label>
          <MetricPicker evaluators={evaluators} chosen={form.metrics}
            onToggle={(name) => set({ metrics: form.metrics.includes(name) ? form.metrics.filter((x) => x !== name) : [...form.metrics, name] })} />
          {pathInput('label_file', '文字指标的 Label 文件（PaddleOCR 格式，可选）', '/data/datasets/xxx/Label.txt')}
          {pathInput('gt_dir', 'GT 目录（可选，按文件名与样本配对）', '/data/datasets/xxx/HR')}
          {pathInput('lq_dir', 'LQ 目录（可选，与结果并排展示，并计算一次 LQ 基线指标）', '/data/datasets/xxx/LR')}
          <div className="row" style={{ gap: 12 }}>
            <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">评测服务器</span>
              <select className="inp" value={form.server_id ?? ''} onChange={(e) => set({ server_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">结果所在服务器</option>
                {(servers.data ?? []).map((sv) => <option key={sv.id} value={sv.id} disabled={!sv.ssh_user}>{sv.name}{sv.ssh_user ? '' : '（未填 SSH 用户）'}</option>)}
              </select>
            </label>
            <label className="field" style={{ flex: '2 1 0', minWidth: 0 }}><span className="lbl">评测服务器上的路径（结果拷贝到这里）</span>
              <input className="inp mono" style={{ fontSize: 13 }} placeholder="/data/eval" disabled={!form.server_id} value={form.server_path ?? ''} onChange={(e) => set({ server_path: e.target.value })} />
            </label>
          </div>
          <div className="row" style={{ gap: 12 }}>
            <label className="field" style={{ flex: '1 1 0', minWidth: 0 }}><span className="lbl">卡数（0 ~ 8）</span>
              <input className="inp mono" inputMode="numeric" value={form.num_devices} onChange={(e) => set({ num_devices: Math.max(0, Math.min(8, Number(e.target.value) || 0)) })} />
            </label>
            <label className="field" style={{ flex: '3 1 0', minWidth: 0 }}><span className="lbl">备注（可选）</span><input className="inp" value={form.note ?? ''} onChange={(e) => set({ note: e.target.value })} /></label>
          </div>
          <div className="notice">Label、GT、LQ 路径都在评测服务器上（没有指定评测服务器时在结果所在服务器上）。指定评测服务器后，中心服务通过 SSH 把结果目录拷贝到“评测服务器上的路径/result_&lt;结果集 ID&gt;”，再在评测服务器上运行评测；两台服务器都需要填写 SSH 用户。</div>
          {err && <div className="notice err">{err}</div>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => setForm(null)}>返回</button>
            <button type="submit" className="btn pri" disabled={!form.name.trim() || form.metrics.length === 0}>{editing ? '保存' : '创建配置'}</button>
          </div>
        </form>
      )}
    </Modal>
  )
}
