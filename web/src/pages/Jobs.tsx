import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { jobsApi } from '../api/jobs'
import { serversApi } from '../api/servers'
import type { Accelerator, Job, JobStatus, Server } from '../api/types'
import { Badge, ErrorNote, MockBadge, Modal } from '../components/common'
import { IconPlus } from '../components/Icons'
import { ACC_LOOK, JOB_LOOK, clock, duration, schedulableIdle } from '../lib/format'
import { useMock, usePoll } from '../lib/hooks'

const TABS: ('all' | JobStatus)[] = ['all', 'queued', 'starting', 'running', 'succeeded', 'failed', 'cancelled', 'lost']
const ORDER: Record<JobStatus, number> = { queued: 0, starting: 1, running: 1, succeeded: 2, failed: 2, cancelled: 2, lost: 1 }

function sortJobs(js: Job[]): Job[] {
  return [...js].sort((a, b) => ORDER[a.status] - ORDER[b.status]
    || (a.status === 'queued' && b.status === 'queued' ? b.priority - a.priority || a.created_at.localeCompare(b.created_at) : b.id - a.id))
}

function allocText(j: Job): string {
  if (!j.assigned_server_name) return j.server_id ? `指定服务器 #${j.server_id}` : '-'
  return `${j.assigned_server_name} [${j.device_indices.join(',')}]`
}

function timeText(j: Job): [string, string] {
  if (j.status === 'queued') return [`${clock(j.created_at).slice(0, 5)} 提交`, `${j.queue_position ? `第 ${j.queue_position} 位 · ` : ''}已等待 ${duration(j.created_at)}`]
  if (j.finished_at) return [`${clock(j.finished_at).slice(0, 5)} 结束`, j.exit_code !== null && j.status === 'failed' ? `退出码 ${j.exit_code}` : `用时 ${duration(j.started_at, j.finished_at)}`]
  if (j.status === 'lost') return [`${clock(j.started_at).slice(0, 5)} 启动`, 'Agent 离线，恢复后对账']
  return [`${clock(j.started_at).slice(0, 5)} 启动`, `已运行 ${duration(j.started_at)}`]
}

export default function JobsPage() {
  const [params, setParams] = useSearchParams()
  const [tab, setTab] = useState<'all' | JobStatus>('all')
  const [submitter, setSubmitter] = useState('')
  const [group, setGroup] = useState('')
  const [showSubmit, setShowSubmit] = useState(false)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const jobs = usePoll(() => jobsApi.list(), [], 5_000)
  const sched = usePoll(() => jobsApi.scheduler(), [], 30_000)
  const mock = useMock('jobs')
  const schedMock = useMock('scheduler')

  const all = jobs.data?.items ?? []
  const rows = sortJobs(all.filter((j) => (tab === 'all' || j.status === tab) && (!submitter || j.submitter === submitter) && (!group || j.group === group)))
  const selId = Number(params.get('id')) || rows[0]?.id
  const sel = all.find((j) => j.id === selId)
  const submitters = [...new Set(all.map((j) => j.submitter).filter(Boolean))] as string[]
  const groups = [...new Set(all.map((j) => j.group).filter(Boolean))] as string[]

  const act = async (fn: () => Promise<Job>, select = false) => {
    setActionErr(null)
    try {
      const j = await fn()
      await jobs.reload()
      if (select) setParams({ id: String(j.id) }, { replace: true })
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.detail : String(e))
    }
  }
  const strict = sched.data?.strict_order ?? false

  return (
    <main className="main">
      <header className="page-head">
        <div className="grow">
          <h1>任务队列</h1>
          <div className="lbl" style={{ marginTop: 4 }}>按优先级从高到低、提交时间从早到晚调度；任务只在单台服务器内分配卡</div>
        </div>
        <MockBadge show={mock} what="任务" />
        {schedMock ? <span className="lbl" title="后端暂未提供调度模式接口，由服务端环境变量 GNM_SCHEDULE_STRICT 配置">调度模式由服务端配置</span> : (
        <button type="button" className="btn" aria-pressed={strict} disabled={!sched.data}
          onClick={async () => sched.setData(await jobsApi.setScheduler({ strict_order: !strict }))}
          title="开启后排在前面的任务放不下时，后面的任务也不会先运行">
          <span style={{ width: 30, height: 18, borderRadius: 9, padding: 2, display: 'flex', background: strict ? 'var(--accent)' : 'var(--control)', justifyContent: strict ? 'flex-end' : 'flex-start' }}>
            <span style={{ width: 14, height: 14, borderRadius: 7, background: '#FFFFFF' }} />
          </span>
          严格按顺序
        </button>)}
        <button className="btn pri" type="button" onClick={() => setShowSubmit(true)}><IconPlus />提交任务</button>
      </header>

      <div className="row wrap" style={{ gap: 6 }}>
        {TABS.map((t) => (
          <button key={t} type="button" className={tab === t ? 'chip on' : 'chip'} onClick={() => setTab(t)} aria-pressed={tab === t}>
            {t === 'all' ? '全部' : JOB_LOOK[t].name} <span className="mono" style={{ opacity: 0.7, marginLeft: 2 }}>{t === 'all' ? all.length : all.filter((j) => j.status === t).length}</span>
          </button>
        ))}
        <span className="grow" />
        <select className="inp" aria-label="提交人" value={submitter} onChange={(e) => setSubmitter(e.target.value)}>
          <option value="">全部提交人</option>
          {submitters.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select className="inp" aria-label="运行组" value={group} onChange={(e) => setGroup(e.target.value)}>
          <option value="">全部运行组</option>
          {groups.map((g) => <option key={g}>{g}</option>)}
        </select>
      </div>
      <ErrorNote error={jobs.error || actionErr} />

      <div className="split">
        <section className="card table-box" style={{ flex: '3 1 680px', minWidth: 0 }}>
          <table className="tbl">
            <thead><tr>
              <th>ID</th><th>任务</th><th>状态</th><th className="num">卡数</th><th>运行组 / 分配</th><th style={{ textAlign: 'center' }}>优先级</th><th>时间</th><th className="num">操作</th>
            </tr></thead>
            <tbody>
              {rows.map((j) => {
                const [t1, t2] = timeText(j)
                const stop = (fn: () => void) => (e: React.MouseEvent) => { e.stopPropagation(); fn() }
                return (
                  <tr key={j.id} className={j.id === selId ? 'click sel' : 'click'} onClick={() => setParams({ id: String(j.id) }, { replace: true })}>
                    <td className="mono lbl">#{j.id}</td>
                    <td style={{ maxWidth: 220 }}>
                      <div className="ellipsis" style={{ fontWeight: 500 }}>{j.name}</div>
                      <div className="lbl">{j.submitter ?? '-'}</div>
                    </td>
                    <td><Badge bg={JOB_LOOK[j.status].bg} fg={JOB_LOOK[j.status].fg}>{JOB_LOOK[j.status].name}</Badge></td>
                    <td className="mono num">{j.num_devices}</td>
                    <td><div>{j.group ?? '-'}</div><div className="lbl mono">{allocText(j)}</div></td>
                    <td style={{ textAlign: 'center' }}>
                      {j.status === 'queued' ? (
                        <span className="stepper">
                          <button type="button" aria-label="降低优先级" onClick={stop(() => act(() => jobsApi.setPriority(j.id, j.priority - 1)))}>−</button>
                          <span>{j.priority}</span>
                          <button type="button" aria-label="提高优先级" onClick={stop(() => act(() => jobsApi.setPriority(j.id, j.priority + 1)))}>+</button>
                        </span>
                      ) : <span className="mono lbl">{j.priority}</span>}
                    </td>
                    <td><div>{t1}</div><div className="lbl">{t2}</div></td>
                    <td className="num">
                      {(j.status === 'queued' || j.status === 'running' || j.status === 'starting') && (
                        <button type="button" className="btn sm danger" onClick={stop(() => {
                          if (j.status === 'queued' || window.confirm(`取消运行中的任务 #${j.id}？会终止整个进程组。`)) act(() => jobsApi.cancel(j.id))
                        })}>取消</button>
                      )}
                      {j.status === 'lost' && (
                        <button type="button" className="btn sm danger" onClick={stop(() => {
                          if (window.confirm(`任务 #${j.id} 所在服务器失联，强制标记为已取消？进程可能仍在运行。`)) act(() => jobsApi.cancel(j.id, true))
                        })}>强制取消</button>
                      )}
                      {['succeeded', 'failed', 'cancelled'].includes(j.status) && (
                        <button type="button" className="btn sm" onClick={stop(() => act(() => jobsApi.requeue(j.id), true))}>重新排队</button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {!jobs.loading && rows.length === 0 && <div className="empty">这个状态下没有任务</div>}
        </section>
        {sel && <JobDetail key={sel.id} j={sel} />}
      </div>

      {showSubmit && <SubmitDrawer onClose={() => setShowSubmit(false)} onSubmitted={(j) => { setShowSubmit(false); setTab('all'); jobs.reload(); setParams({ id: String(j.id) }, { replace: true }) }} />}
    </main>
  )
}

function JobDetail({ j }: { j: Job }) {
  const [log, setLog] = useState('')
  const [offset, setOffset] = useState(0)
  const [size, setSize] = useState(0)
  const [follow, setFollow] = useState(true)
  const pre = useRef<HTMLPreElement>(null)
  const offRef = useRef(0)
  const active = j.status === 'running' || j.status === 'starting'

  useEffect(() => {
    let stop = false
    const pull = async () => {
      if (!j.started_at) return
      try {
        const c = await jobsApi.log(j.id, offRef.current)
        if (stop) return
        setSize(c.size)
        if (!c.data) return
        offRef.current = c.next_offset
        setOffset(c.next_offset)
        setLog((s) => s + c.data)
      } catch {
        // 日志拉取失败时保留已有内容，下一轮重试
      }
    }
    pull()
    if (!active) return () => { stop = true }
    const t = setInterval(pull, 2000)
    return () => { stop = true; clearInterval(t) }
  }, [j.id, j.started_at, active])

  useEffect(() => {
    if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight
  }, [log, follow])

  const done = !!j.finished_at
  const steps = [
    { name: '提交', t: j.created_at, on: true },
    { name: '启动', t: j.started_at, on: !!j.started_at },
    { name: '结束', t: j.finished_at, on: done },
  ]
  return (
    <aside className="card col" style={{ flex: '2 1 380px', minWidth: 0 }}>
      <div className="col" style={{ padding: 18, gap: 6, borderBottom: '1px solid var(--border-soft)' }}>
        <div className="row">
          <span className="mono lbl">#{j.id}</span>
          <h2 className="grow ellipsis" style={{ fontSize: 17 }}>{j.name}</h2>
          <Badge bg={JOB_LOOK[j.status].bg} fg={JOB_LOOK[j.status].fg}>{JOB_LOOK[j.status].name}</Badge>
        </div>
        <div className="lbl">
          {j.submitter ?? '-'} 提交 · {j.group ?? '不限运行组'}{j.accelerator ? ` · 仅 ${ACC_LOOK[j.accelerator].name}` : ''} · {j.num_devices} 卡 · 优先级 {j.priority}
          {j.requeued_from ? ` · 由 #${j.requeued_from} 重新排队` : ''}
        </div>
        {j.status === 'queued' && (
          <div className="notice warn" style={{ marginTop: 6 }}>
            队列第 {j.queue_position ?? '-'} 位{j.wait_reason ? `：${j.wait_reason}` : '，等待有足够空闲卡的服务器'}
          </div>
        )}
        {j.error && <div className="notice err" style={{ marginTop: 6 }}>{j.error}</div>}
      </div>
      <div style={{ padding: '14px 18px', display: 'grid', gridTemplateColumns: '88px minmax(0, 1fr)', gap: '8px 12px', fontSize: 13, borderBottom: '1px solid var(--border-soft)' }}>
        <span className="lbl">命令</span><code className="mono" style={{ background: 'var(--bg)', padding: '6px 8px', borderRadius: 4, fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{j.command}</code>
        <span className="lbl">工作目录</span><span className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>{j.workdir ?? 'Agent 运行用户的家目录'}</span>
        <span className="lbl">环境变量</span><span className="mono" style={{ fontSize: 12, whiteSpace: 'pre-line' }}>{Object.entries(j.env).map(([k, v]) => `${k}=${v}`).join('\n') || '-'}</span>
        <span className="lbl">分配</span><span className="mono" style={{ fontSize: 12 }}>{allocText(j)}</span>
        <span className="lbl">可见卡号</span><span className="mono" style={{ fontSize: 12 }}>{j.device_indices.length ? j.device_indices.join(',') : '-'}</span>
        <span className="lbl">PID / 退出码</span><span className="mono" style={{ fontSize: 12 }}>{j.pid ?? '-'} / {j.exit_code ?? '-'}</span>
      </div>
      <div className="row" style={{ padding: '14px 18px', gap: 0, borderBottom: '1px solid var(--border-soft)', alignItems: 'flex-start' }}>
        {steps.map((s, i) => (
          <div key={s.name} className="col" style={{ flex: '1 1 0', gap: 6 }}>
            <div className="row" style={{ gap: 0 }}>
              <span className="dot" style={{ width: 10, height: 10, borderRadius: 5, background: s.on ? 'var(--accent)' : 'var(--control)' }} />
              {i < steps.length - 1 && <span className="grow" style={{ height: 2, background: steps[i + 1].on ? 'var(--accent)' : 'var(--control)' }} />}
            </div>
            <span style={{ fontSize: 12, fontWeight: 500 }}>{s.name}</span>
            <span className="lbl mono" style={{ fontSize: 11 }}>{s.on ? clock(s.t) : i === 1 && j.status === 'queued' ? '排队中' : '-'}</span>
          </div>
        ))}
      </div>
      <div className="col" style={{ padding: '14px 18px 18px', gap: 8 }}>
        <div className="row">
          <span className="grow" style={{ fontWeight: 500 }}>日志</span>
          <label className="row" style={{ gap: 6, fontSize: 13, color: 'var(--ink-2)' }}>
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />跟随最新输出
          </label>
        </div>
        <pre ref={pre} className="log">{j.started_at ? log || '（暂无输出）' : '（任务尚未启动，暂无日志）'}</pre>
        <div className="lbl">按偏移量增量拉取{active ? '，每 2 秒更新' : ''}，已读取 {(offset / 1024).toFixed(1)} / {(size / 1024).toFixed(1)} KB</div>
      </div>
    </aside>
  )
}

function SubmitDrawer({ onClose, onSubmitted }: { onClose: () => void; onSubmitted: (j: Job) => void }) {
  const servers = usePoll(() => serversApi.list({ include_devices: false }), [], 10_000)
  const [name, setName] = useState('')
  const [command, setCommand] = useState('')
  const [workdir, setWorkdir] = useState('')
  const [acc, setAcc] = useState<'' | Accelerator>('')
  const [env, setEnv] = useState('')
  const [group, setGroup] = useState<string | null>(null)
  const [count, setCount] = useState(1)
  const [serverId, setServerId] = useState('')
  const [priority, setPriority] = useState('0')
  const [submitter, setSubmitter] = useState(() => localStorage.getItem('gnm.submitter') ?? '')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const list: Server[] = servers.data ?? []
  const groupNames = [...new Set(list.map((s) => s.group).filter(Boolean))] as string[]
  const g = group ?? groupNames[0] ?? ''
  const gName = g || '所有运行组'
  // 与调度器一致：在线、参与调度、满足运行组和类型，空闲卡足够时选剩余最少的一台
  const inGroup = list.filter((s) => (!g || s.group === g) && (!acc || s.accelerator === acc))
  const fit = inGroup.filter((s) => s.status === 'online' && s.schedulable && schedulableIdle(s) >= count && (!serverId || String(s.id) === serverId))
    .sort((a, b) => a.idle_device_count - b.idle_device_count)
  const capable = inGroup.some((s) => s.device_count >= count && (!serverId || String(s.id) === serverId))
  const hint = !capable
    ? `${gName}里没有卡数达到 ${count} 张的服务器，提交会被拒绝`
    : fit.length ? (count === 0 ? `不需要卡，预计调度到 ${fit[0].name}` : `预计立即调度到 ${fit[0].name}（空闲 ${fit[0].idle_device_count} 张，是能放下的服务器里剩余最少的）`)
      : `${gName}当前没有一台服务器有 ${count} 张空闲卡，提交后将排队`
  const ok = capable && fit.length > 0

  const submit = async () => {
    setErr(null)
    const envObj: Record<string, string> = {}
    for (const line of env.split('\n').map((l) => l.trim()).filter(Boolean)) {
      const i = line.indexOf('=')
      if (i <= 0) return setErr(`环境变量格式不对：${line}`)
      envObj[line.slice(0, i)] = line.slice(i + 1)
    }
    setBusy(true)
    try {
      localStorage.setItem('gnm.submitter', submitter)
      onSubmitted(await jobsApi.create({
        name: name.trim() || null, command, workdir: workdir || null, env: envObj, num_devices: count, group: g || null, accelerator: acc || null,
        server_id: serverId ? Number(serverId) : null, priority: Math.max(-100, Math.min(100, Number(priority) || 0)), submitter: submitter || null,
      }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="提交任务" onClose={onClose} drawer>
      <form className="col" style={{ gap: 14, flexGrow: 1 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        <label className="field"><span className="lbl">名称（不填则取命令第一行）</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
        <label className="field"><span className="lbl">命令</span><textarea className="inp mono" rows={3} style={{ fontSize: 13 }} value={command} onChange={(e) => setCommand(e.target.value)} placeholder="bash scripts/train.sh --config configs/v4.yaml" required /></label>
        <label className="field"><span className="lbl">工作目录（不填为 Agent 运行用户的家目录）</span><input className="inp mono" style={{ fontSize: 13 }} value={workdir} onChange={(e) => setWorkdir(e.target.value)} placeholder="/data/projects/xxx" /></label>
        <label className="field"><span className="lbl">环境变量（每行一个 KEY=VALUE）</span><textarea className="inp mono" rows={2} style={{ fontSize: 13 }} value={env} onChange={(e) => setEnv(e.target.value)} /></label>
        <div className="field">
          <span className="lbl">运行组</span>
          <div className="seg-group">
            {groupNames.map((n) => <button key={n} type="button" className={g === n ? 'seg on' : 'seg'} onClick={() => { setGroup(n); setServerId('') }}>{n}</button>)}
            <button type="button" className={g === '' ? 'seg on' : 'seg'} onClick={() => { setGroup(''); setServerId('') }}>不限</button>
          </div>
        </div>
        <div className="field">
          <span className="lbl">加速卡类型</span>
          <div className="seg-group">
            {([['', '不限'], ['gpu', 'GPU'], ['npu', 'NPU']] as const).map(([k, n]) => (
              <button key={k} type="button" className={acc === k ? 'seg on' : 'seg'} onClick={() => { setAcc(k); setServerId('') }}>{n}</button>
            ))}
          </div>
        </div>
        <div className="row" style={{ gap: 12, alignItems: 'flex-end' }}>
          <div className="field">
            <span className="lbl">卡数</span>
            <span className="stepper" style={{ height: 36 }}>
              <button type="button" aria-label="减少卡数" style={{ width: 36, height: 36 }} onClick={() => setCount(Math.max(0, count - 1))}>−</button>
              <span style={{ width: 36, fontWeight: 500 }}>{count}</span>
              <button type="button" aria-label="增加卡数" style={{ width: 36, height: 36 }} onClick={() => setCount(Math.min(16, count + 1))}>+</button>
            </span>
          </div>
          <label className="field grow"><span className="lbl">指定服务器（可选）</span>
            <select className="inp" value={serverId} onChange={(e) => setServerId(e.target.value)}>
              <option value="">由调度器选择</option>
              {inGroup.map((s) => <option key={s.id} value={s.id}>{s.name}（空闲 {schedulableIdle(s)}）</option>)}
            </select>
          </label>
          <label className="field" style={{ width: 90 }}><span className="lbl">优先级</span><input className="inp mono" value={priority} onChange={(e) => setPriority(e.target.value)} inputMode="numeric" /></label>
        </div>
        <div className={ok ? 'notice ok' : 'notice warn'}>
          <div style={{ fontWeight: 500 }}>{hint}</div>
          <div className="lbl" style={{ color: 'inherit', marginTop: 4 }}>{gName}可调度空闲卡：{inGroup.map((s) => `${s.name} ${schedulableIdle(s)}`).join('、') || '无'}</div>
        </div>
        <label className="field"><span className="lbl">提交人</span><input className="inp" value={submitter} onChange={(e) => setSubmitter(e.target.value)} /></label>
        {err && <div className="notice err">{err}</div>}
        <div className="grow" />
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="submit" className="btn pri" disabled={busy || !command.trim()}>{busy ? '提交中' : '提交到队列'}</button>
        </div>
      </form>
    </Modal>
  )
}
