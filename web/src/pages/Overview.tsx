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
          sub={offline.length ? `${offline.length} 台不在线：${offline.slice(0, 3).map((s) => s.name).join('、')}${offline.length > 3 ? ' 等' : ''}` : '全部在线'} />
        <Kpi label="可调度空闲卡" value={schedIdle} unit={`/ ${ov?.devices_total ?? '-'}`}
          sub={ov ? Object.entries(ov.by_accelerator).map(([k, v]) => `${ACC_LOOK[k]?.name ?? k} 空闲 ${v.idle_devices}`).join(' · ') || '暂无加速卡' : '-'} />
        <Kpi label="运行中任务" value={ov?.jobs_running ?? running.length} unit="个" sub={`占用 ${running.reduce((a, j) => a + j.num_devices, 0)} 张卡${ov?.jobs_lost ? ` · ${ov.jobs_lost} 个失联` : ''}`} />
        <Kpi label="排队任务" value={ov?.jobs_queued ?? queued.length} unit="个" sub={queued.length ? `最长已等待 ${duration(queued.reduce((m, j) => (j.created_at < m ? j.created_at : m), queued[0].created_at))}` : '队列为空'} />
      </section>

      <div className="split">
        <section className="card" style={{ flex: '2 1 560px', minWidth: 0, paddingBottom: 8 }}>
          <div className="card-head wrap">
            <h2 className="grow">卡占用一览</h2>
            {(['idle', 'job', 'busy', 'unhealthy', 'offline'] as const).map((k) => (
              <span key={k} className="row" style={{ gap: 6, fontSize: 12, color: 'var(--ink-2)' }}>
                <span className="cell" style={{ width: 12, height: 12, borderRadius: 3, background: DEVICE_LOOK[k].bg, borderColor: DEVICE_LOOK[k].bd }} />
                {DEVICE_LOOK[k].name}
              </span>
            ))}
          </div>
          <div className="table-box">
            {list.length === 0 && !servers.loading && <div className="empty">还没有服务器，<Link to="/servers">去添加</Link></div>}
            {list.map((s) => {
              const acc = s.accelerator ? ACC_LOOK[s.accelerator] : null
              return (
                <Link key={s.id} to={`/servers?id=${s.id}`} className="list-row" style={{ gap: 14, color: 'var(--ink)', minWidth: 640, padding: '8px 18px' }}>
                  <span className="dot" style={{ background: STATUS_LOOK[s.status].dot }} title={STATUS_LOOK[s.status].name} />
                  <span className="mono ellipsis" style={{ width: 130, fontSize: 13 }}>{s.name}</span>
                  {acc ? <Badge bg={acc.bg} fg={acc.fg}>{acc.name}</Badge> : <Badge bg="#ECEDEB" fg="#5E646D">-</Badge>}
                  <span className="lbl ellipsis" style={{ width: 140 }}>{s.models.join(' / ') || '未识别'}</span>
                  <span className="row grow" style={{ gap: 4 }}>
                    {s.devices.map((d) => {
                      const look = DEVICE_LOOK[deviceState(s, d)]
                      return <span key={d.index} className="cell" title={`卡 ${d.index} · ${look.name}`} style={{ background: look.bg, borderColor: look.bd }} />
                    })}
                  </span>
                  <span className="lbl" style={{ width: 90, textAlign: 'right' }}>{s.group ?? '未分组'}{s.schedulable ? '' : ' · 停调度'}</span>
                  <span className="mono" style={{ width: 64, textAlign: 'right', fontSize: 13, color: s.status === 'online' && s.idle_device_count ? '#1B5E3A' : 'var(--muted)' }}>
                    {s.status === 'online' ? `${s.idle_device_count}/${s.device_count}` : STATUS_LOOK[s.status].name}
                  </span>
                </Link>
              )
            })}
          </div>
        </section>

        <div className="col" style={{ flex: '1 1 320px', minWidth: 0, gap: 20 }}>
          <section className="card" style={{ padding: 18 }}>
            <div className="row" style={{ marginBottom: 12 }}>
              <h2 className="grow">运行组空闲卡</h2>
              <span className="lbl">在线且参与调度</span>
            </div>
            <div className="col" style={{ gap: 14 }}>
              {[...groups.entries()].map(([name, ss]) => {
                const total = ss.reduce((a, s) => a + s.device_count, 0)
                const idle = ss.reduce((a, s) => a + schedulableIdle(s), 0)
                const busy = ss.reduce((a, s) => a + (s.status === 'online' ? s.device_count - s.idle_device_count : 0), 0)
                return (
                  <div key={name} className="col" style={{ gap: 6 }}>
                    <div className="row">
                      <span className="grow" style={{ fontWeight: 500 }}>{name}</span>
                      <span className="mono" style={{ fontSize: 13 }}>{idle}<span style={{ color: 'var(--faint)' }}> / {total} 空闲</span></span>
                    </div>
                    <div className="bar" style={{ height: 8, display: 'flex' }}>
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
              <Link to="/jobs" style={{ fontSize: 13 }}>查看队列</Link>
            </div>
            {queued.slice(0, 5).map((j) => (
              <div key={j.id} className="list-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 3 }}>
                <div className="row">
                  <span className="mono lbl">#{j.id}</span>
                  <span className="grow ellipsis" style={{ fontWeight: 500 }}>{j.name}</span>
                  <span className="mono" style={{ fontSize: 13 }}>{j.num_devices} 卡</span>
                </div>
                <div className="lbl">{j.group ?? '不限运行组'} · 优先级 {j.priority} · 已等待 {duration(j.created_at)}{j.wait_reason ? ` · ${j.wait_reason}` : ''}</div>
              </div>
            ))}
            {queued.length === 0 && <div className="list-row lbl">队列为空</div>}
          </section>
        </div>
      </div>

      <div className="split">
        <section className="card" style={{ flex: '1 1 420px', minWidth: 0, paddingBottom: 6 }}>
          <div className="card-head">
            <h2 className="grow">最近结束的任务</h2>
            <Link to="/jobs" style={{ fontSize: 13 }}>全部任务</Link>
          </div>
          {finished.map((j) => (
            <div key={j.id} className="list-row">
              <Badge bg={JOB_LOOK[j.status].bg} fg={JOB_LOOK[j.status].fg}>{JOB_LOOK[j.status].name}</Badge>
              <span className="grow ellipsis">{j.name}</span>
              <span className="lbl mono">{j.assigned_server_name ?? '-'}</span>
              <span className="lbl" style={{ width: 72, textAlign: 'right' }}>{relTime(j.finished_at)}</span>
            </div>
          ))}
          {finished.length === 0 && <div className="list-row lbl">暂无</div>}
        </section>
        <section className="card" style={{ flex: '1 1 420px', minWidth: 0, paddingBottom: 6 }}>
          <div className="card-head">
            <h2 className="grow">最近评测</h2>
            <MockBadge show={resultsMock} what="评测" />
            <Link to="/results" style={{ fontSize: 13 }}>结果与评测</Link>
          </div>
          {(evals.data ?? []).slice(0, 4).map((e) => (
            <div key={e.id} className="list-row">
              <Badge bg={EVAL_LOOK[e.status].bg} fg={EVAL_LOOK[e.status].fg}>{EVAL_LOOK[e.status].name}</Badge>
              <span className="grow ellipsis">{e.result_set_name}</span>
              <span className="mono" style={{ fontSize: 13, color: 'var(--ink-2)' }}>
                {e.values ? e.metrics.slice(0, 1).map((k) => `${metricLabel(evaluators.data, k)} ${fmtMetric(e.values?.[k])}`).join('') : e.error ? '失败' : '-'}
              </span>
            </div>
          ))}
        </section>
      </div>
    </main>
  )
}

function Kpi({ label, value, unit, sub }: { label: string; value: number | string; unit: string; sub: string }) {
  return (
    <div className="card kpi">
      <div className="lbl" style={{ fontSize: 13 }}>{label}</div>
      <div className="row" style={{ alignItems: 'baseline', gap: 6 }}>
        <span className="kpi-value">{value}</span>
        <span className="lbl" style={{ fontSize: 14 }}>{unit}</span>
      </div>
      <div className="lbl">{sub}</div>
    </div>
  )
}
