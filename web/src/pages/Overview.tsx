import { Link } from 'react-router-dom'
import { serversApi } from '../api/servers'
import { jobsApi } from '../api/jobs'
import { resultsApi } from '../api/results'
import type { Server } from '../api/types'
import { Badge, ErrorNote, MockBadge } from '../components/common'
import { IconRefresh } from '../components/Icons'
import { ACC_LOOK, DEVICE_LOOK, EVAL_LOOK, JOB_LOOK, STATUS_LOOK, clock, deviceState, duration, relTime, schedulableIdle } from '../lib/format'
import { useMock, usePoll } from '../lib/hooks'
import { fmtMetric, metricLabel } from './Results'

export default function OverviewPage() {
  const servers = usePoll(() => serversApi.list(), [])
  const overview = usePoll(() => serversApi.overview(), [])
  const jobs = usePoll(() => jobsApi.list(), [], 5_000)
  const evals = usePoll(() => resultsApi.evaluations(), [], 30_000)
  const evaluators = usePoll(() => resultsApi.evaluators(), [], 0)
  const jobsMock = useMock('jobs')
  const resultsMock = useMock('results')

  const list = servers.data ?? []
  const ov = overview.data
  const js = jobs.data?.items ?? []
  const running = js.filter((j) => j.status === 'running' || j.status === 'starting')
  const queued = js.filter((j) => j.status === 'queued').sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at))
  const finished = js.filter((j) => j.finished_at).sort((a, b) => b.finished_at!.localeCompare(a.finished_at!)).slice(0, 4)
  const offline = list.filter((s) => s.status !== 'online')
  const schedIdle = list.reduce((a, s) => a + schedulableIdle(s), 0)

  const groups = new Map<string, Server[]>()
  list.forEach((s) => {
    const k = s.group ?? '未分组'
    groups.set(k, [...(groups.get(k) ?? []), s])
  })

  const reloadAll = () => {
    servers.reload()
    overview.reload()
    jobs.reload()
  }

  return (
    <main className="main">
      <header className="page-head">
        <div className="grow">
          <h1>总览</h1>
          <div className="lbl" style={{ marginTop: 4 }}>状态每 10 秒刷新 · 最后更新 {servers.data ? clock(new Date().toISOString()) : '-'}</div>
        </div>
        <MockBadge show={jobsMock} what="任务" />
        <button className="btn" type="button" onClick={reloadAll}><IconRefresh />刷新</button>
      </header>
      <ErrorNote error={servers.error || overview.error} />

      <section className="kpis">
        <Kpi label="在线服务器" value={ov?.servers_online ?? '-'} unit={`/ ${ov?.servers_total ?? '-'}`}
          accent="#2E8B57"
          sub={offline.length ? `${offline.length} 台不在线：${offline.slice(0, 3).map((s) => s.name).join('、')}${offline.length > 3 ? ' 等' : ''}` : '全部在线'} />
        <Kpi label="可调度空闲卡" value={schedIdle} unit={`/ ${ov?.devices_total ?? '-'}`}
          accent="var(--accent)"
          sub={ov ? Object.entries(ov.by_accelerator).map(([k, v]) => `${ACC_LOOK[k]?.name ?? k} 空闲 ${v.idle_devices}`).join(' · ') || '暂无加速卡' : '-'} />
        <Kpi label="运行中任务" value={ov?.jobs_running ?? running.length} unit="个"
          accent="#3A4048"
          sub={`占用 ${running.reduce((a, j) => a + j.num_devices, 0)} 张卡${ov?.jobs_lost ? ` · ${ov.jobs_lost} 个失联` : ''}`} />
        <Kpi label="排队任务" value={ov?.jobs_queued ?? queued.length} unit="个"
          accent={queued.length ? '#C46A0B' : 'var(--control)'}
          sub={queued.length ? `最长已等待 ${duration(queued.reduce((m, j) => (j.created_at < m ? j.created_at : m), queued[0].created_at))}` : '队列为空'} />
      </section>

      <div className="split">
        <section className="card" style={{ flex: '2 1 560px', minWidth: 0, paddingBottom: 6 }}>
          <div className="card-head wrap">
            <h2 className="grow">卡占用一览</h2>
            {(['idle', 'job', 'busy', 'unhealthy', 'offline'] as const).map((k) => (
              <span key={k} className="row shrink-0" style={{ gap: 6, fontSize: 12, color: 'var(--ink-2)' }}>
                <span className="cell" style={{ width: 12, height: 12, borderRadius: 3, background: DEVICE_LOOK[k].bg, borderColor: DEVICE_LOOK[k].bd }} />
                {DEVICE_LOOK[k].name}
              </span>
            ))}
          </div>
          <div className="table-box">
            {list.length === 0 && !servers.loading && <div className="empty">还没有服务器，<Link to="/servers">去添加</Link></div>}
            {list.map((s) => {
              const acc = s.accelerator ? ACC_LOOK[s.accelerator] : null
              const modelText = s.models.join(' / ') || '未识别'
              const groupText = `${s.group ?? '未分组'}${s.schedulable ? '' : ' · 停调度'}`
              return (
                <Link key={s.id} to={`/servers?id=${s.id}`} className="list-row" style={{ gap: 12, color: 'var(--ink)', minWidth: 640, padding: '9px 18px' }}>
                  <span className="dot" style={{ background: STATUS_LOOK[s.status].dot }} title={STATUS_LOOK[s.status].name} />
                  <span className="mono ellipsis" style={{ flex: '0 0 156px', fontSize: 13, fontWeight: 500 }} title={s.name}>{s.name}</span>
                  {acc ? <Badge bg={acc.bg} fg={acc.fg}>{acc.name}</Badge> : <Badge bg="#ECEDEB" fg="#5E646D">-</Badge>}
                  <span className="lbl ellipsis" style={{ flex: '0 1 150px', minWidth: 88 }} title={modelText}>{modelText}</span>
                  <span className="row wrap" style={{ flex: '1 1 176px', minWidth: 112, gap: 4 }}>
                    {s.devices.map((d) => {
                      const look = DEVICE_LOOK[deviceState(s, d)]
                      return <span key={d.index} className="cell" title={`卡 ${d.index} · ${look.name}`} style={{ background: look.bg, borderColor: look.bd }} />
                    })}
                  </span>
                  <span className="lbl ellipsis" style={{ flex: '0 0 132px', textAlign: 'right' }} title={groupText}>{groupText}</span>
                  <span className="mono shrink-0 nowrap" style={{ width: 56, textAlign: 'right', fontSize: 13, fontWeight: 500, color: s.status === 'online' && s.idle_device_count ? '#1B5E3A' : 'var(--muted)' }}>
                    {s.status === 'online' ? `${s.idle_device_count}/${s.device_count}` : STATUS_LOOK[s.status].name}
                  </span>
                </Link>
              )
            })}
          </div>
        </section>

        <div className="col" style={{ flex: '1 1 320px', minWidth: 0, gap: 18 }}>
          <section className="card" style={{ padding: '16px 18px' }}>
            <div className="row" style={{ marginBottom: 12 }}>
              <h2 className="grow">运行组空闲卡</h2>
              <span className="lbl shrink-0">在线且参与调度</span>
            </div>
            <div className="col" style={{ gap: 13 }}>
              {[...groups.entries()].map(([name, ss]) => {
                const total = ss.reduce((a, s) => a + s.device_count, 0)
                const idle = ss.reduce((a, s) => a + schedulableIdle(s), 0)
                const busy = ss.reduce((a, s) => a + (s.status === 'online' ? s.device_count - s.idle_device_count : 0), 0)
                return (
                  <div key={name} className="col" style={{ gap: 5 }}>
                    <div className="row" style={{ gap: 10 }}>
                      <span className="grow ellipsis" style={{ fontWeight: 500 }} title={name}>{name}</span>
                      <span className="mono shrink-0 nowrap" style={{ fontSize: 13 }}>{idle}<span style={{ color: 'var(--faint)' }}> / {total} 空闲</span></span>
                    </div>
                    <div className="bar" style={{ height: 7 }}>
                      <div style={{ width: `${total ? (busy / total) * 100 : 0}%`, background: 'var(--accent)' }} />
                    </div>
                    <div className="lbl">{ss.length} 台服务器 · 占用 {busy} 张</div>
                  </div>
                )
              })}
              {groups.size === 0 && <div className="lbl">暂无数据</div>}
            </div>
          </section>

          <section className="card" style={{ paddingBottom: 6 }}>
            <div className="card-head">
              <h2 className="grow">排队中</h2>
              <Link to="/jobs" className="shrink-0" style={{ fontSize: 13 }}>查看队列</Link>
            </div>
            {queued.slice(0, 5).map((j) => {
              const sub = `${j.group ?? '不限运行组'} · 优先级 ${j.priority} · 已等待 ${duration(j.created_at)}${j.wait_reason ? ` · ${j.wait_reason}` : ''}`
              return (
                <div key={j.id} className="list-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 4 }}>
                  <div className="row">
                    <span className="mono lbl shrink-0">#{j.id}</span>
                    <span className="grow ellipsis" style={{ fontWeight: 500 }} title={j.name}>{j.name}</span>
                    <span className="mono shrink-0 nowrap" style={{ fontSize: 13 }}>{j.num_devices} 卡</span>
                  </div>
                  <div className="lbl line-clamp-2" title={sub}>{sub}</div>
                </div>
              )
            })}
            {queued.length === 0 && <div className="list-row lbl">队列为空</div>}
          </section>
        </div>
      </div>

      <div className="split">
        <section className="card" style={{ flex: '1 1 420px', minWidth: 0, paddingBottom: 6 }}>
          <div className="card-head">
            <h2 className="grow">最近结束的任务</h2>
            <Link to="/jobs" className="shrink-0" style={{ fontSize: 13 }}>全部任务</Link>
          </div>
          {finished.map((j) => (
            <div key={j.id} className="list-row">
              <Badge bg={JOB_LOOK[j.status].bg} fg={JOB_LOOK[j.status].fg}>{JOB_LOOK[j.status].name}</Badge>
              <span className="grow ellipsis" style={{ fontWeight: 500 }} title={j.name}>{j.name}</span>
              <span className="lbl mono ellipsis" style={{ maxWidth: 165 }} title={j.assigned_server_name ?? undefined}>{j.assigned_server_name ?? '-'}</span>
              <span className="lbl shrink-0 nowrap" style={{ minWidth: 58, textAlign: 'right' }}>{relTime(j.finished_at)}</span>
            </div>
          ))}
          {finished.length === 0 && <div className="list-row lbl">暂无</div>}
        </section>
        <section className="card" style={{ flex: '1 1 420px', minWidth: 0, paddingBottom: 6 }}>
          <div className="card-head">
            <h2 className="grow">最近评测</h2>
            <MockBadge show={resultsMock} what="评测" />
            <Link to="/results" className="shrink-0" style={{ fontSize: 13 }}>结果与评测</Link>
          </div>
          {(evals.data ?? []).slice(0, 4).map((e) => {
            const metricSummary = e.values ? e.metrics.slice(0, 1).map((k) => `${metricLabel(evaluators.data, k)} ${fmtMetric(e.values?.[k])}`).join('') : e.error ? '失败' : '-'
            return (
              <div key={e.id} className="list-row">
                <Badge bg={EVAL_LOOK[e.status].bg} fg={EVAL_LOOK[e.status].fg}>{EVAL_LOOK[e.status].name}</Badge>
                <span className="grow ellipsis" style={{ fontWeight: 500 }} title={e.result_set_name}>{e.result_set_name}</span>
                <span className="mono shrink-0 nowrap" style={{ fontSize: 13, color: e.error && !e.values ? 'var(--danger)' : 'var(--ink-2)' }} title={e.error ?? undefined}>
                  {metricSummary}
                </span>
              </div>
            )
          })}
          {(evals.data ?? []).length === 0 && <div className="list-row lbl">暂无</div>}
        </section>
      </div>
    </main>
  )
}

function Kpi({ label, value, unit, sub, accent }: { label: string; value: number | string; unit: string; sub: string; accent?: string }) {
  return (
    <div className="card kpi" style={accent ? { borderTop: `3px solid ${accent}` } : undefined}>
      <div className="lbl" style={{ fontSize: 12.5, fontWeight: 500 }}>{label}</div>
      <div className="row" style={{ alignItems: 'baseline', gap: 6 }}>
        <span className="kpi-value">{value}</span>
        <span className="lbl shrink-0" style={{ fontSize: 13.5 }}>{unit}</span>
      </div>
      <div className="lbl line-clamp-2" title={sub}>{sub}</div>
    </div>
  )
}
