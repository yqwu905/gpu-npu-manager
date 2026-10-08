import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { serversApi } from '../api/servers'
import type { Accelerator, GroupBy, Server, ServerQuery, ServerStatus } from '../api/types'
import { Badge, Chips, ErrorNote, Modal, Switch } from '../components/common'
import { IconPlus, IconRefresh, IconSearch } from '../components/Icons'
import { ACC_LOOK, DEVICE_LOOK, STATUS_LOOK, deviceState, gb, num, pct, relTime, schedulableIdle } from '../lib/format'
import { usePoll } from '../lib/hooks'

type By = 'none' | GroupBy
const GROUP_BY: [By, string][] = [['none', '不分组'], ['group', '运行组'], ['owner', '使用人'], ['tag', '标签'], ['accelerator', '加速卡类型'], ['model', '型号'], ['status', '在线状态']]
const KEY_NAME: Partial<Record<By, (k: string) => string>> = {
  accelerator: (k) => ACC_LOOK[k]?.name ?? k,
  status: (k) => STATUS_LOOK[k as ServerStatus]?.name ?? k,
}

export default function ServersPage() {
  const [params, setParams] = useSearchParams()
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const [acc, setAcc] = useState<'all' | Accelerator>('all')
  const [group, setGroup] = useState('')
  const [owner, setOwner] = useState('')
  const [tag, setTag] = useState('')
  const [status, setStatus] = useState<'' | ServerStatus>('')
  const [model, setModel] = useState('')
  const [hasIdle, setHasIdle] = useState(false)
  const [by, setBy] = useState<By>('group')
  const [showAdd, setShowAdd] = useState(false)

  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300)
    return () => clearTimeout(t)
  }, [qInput])

  const query: ServerQuery = {
    q: q || undefined,
    accelerator: acc === 'all' ? undefined : acc,
    group: group ? [group] : undefined,
    owner: owner ? [owner] : undefined,
    tag: tag ? [tag] : undefined,
    status: status || undefined,
    model: model ? [model] : undefined,
    has_idle: hasIdle || undefined,
  }
  const key = JSON.stringify(query)
  const grouped = usePoll(
    async () => (by === 'none' ? [{ key: '全部服务器', servers: await serversApi.list(query) }] : (await serversApi.grouped(by, query)).map((g) => ({ ...g, key: g.key }))),
    [key, by],
  )
  const filters = usePoll(() => serversApi.filters(), [], 60_000)
  const all = usePoll(() => serversApi.list({ include_devices: false }), [], 30_000)

  const groups = grouped.data ?? []
  const shown = useMemo(() => {
    const m = new Map<number, Server>()
    groups.forEach((g) => g.servers.forEach((s) => m.set(s.id, s)))
    return m
  }, [groups])
  const selId = Number(params.get('id')) || [...shown.keys()][0]
  const select = (id: number) => setParams({ id: String(id) }, { replace: true })
  const hasFilter = !!(qInput || acc !== 'all' || group || owner || tag || status || model || hasIdle)
  const clear = () => {
    setQInput(''); setAcc('all'); setGroup(''); setOwner(''); setTag(''); setStatus(''); setModel(''); setHasIdle(false)
  }
  const allList = all.data ?? []
  const fo = filters.data
  const outdated = allList.filter((s) => s.managed && s.agent_outdated && s.deploy?.status !== 'pending' && s.deploy?.status !== 'running')
  const [upgrading, setUpgrading] = useState(false)
  const upgradeAll = async () => {
    if (!window.confirm(`把 ${outdated.length} 台服务器的 Agent 升级到中心服务自带的版本？运行中的任务不受影响。`)) return
    setUpgrading(true)
    try {
      await serversApi.deployOutdated()
    } finally {
      setUpgrading(false)
      grouped.reload(); all.reload()
    }
  }

  return (
    <main className="main">
      <header className="page-head">
        <div className="grow">
          <h1>服务器</h1>
          <div className="lbl" style={{ marginTop: 4 }}>
            {allList.length} 台服务器 · 在线 {allList.filter((s) => s.status === 'online').length} · 加速卡 {allList.reduce((a, s) => a + s.device_count, 0)} 张
          </div>
        </div>
        {outdated.length > 0 && <button className="btn" type="button" onClick={upgradeAll} disabled={upgrading}><IconRefresh />升级 Agent（{outdated.length} 台）</button>}
        <button className="btn pri" type="button" onClick={() => setShowAdd(true)}><IconPlus />添加服务器</button>
      </header>

      <section className="card col" style={{ padding: '14px 16px', gap: 12 }}>
        <div className="row wrap" style={{ gap: 10 }}>
          <label className="search" style={{ flex: '1 1 220px' }}>
            <IconSearch />
            <input aria-label="搜索服务器" placeholder="搜索名称、地址、主机名、备注" value={qInput} onChange={(e) => setQInput(e.target.value)} />
          </label>
          <div className="row" style={{ gap: 6 }}>
            <Chips value={acc} options={[['all', '全部'], ['gpu', 'GPU'], ['npu', 'NPU']]} onChange={setAcc} />
          </div>
          <Select label="运行组" value={group} onChange={setGroup} options={fo?.groups ?? []} />
          <Select label="使用人" value={owner} onChange={setOwner} options={fo?.owners ?? []} />
          <Select label="标签" value={tag} onChange={setTag} options={fo?.tags ?? []} />
          <Select label="型号" value={model} onChange={setModel} options={fo?.models ?? []} />
          <select className="inp" aria-label="在线状态" value={status} onChange={(e) => setStatus(e.target.value as ServerStatus | '')}>
            <option value="">全部状态</option>
            <option value="online">在线</option>
            <option value="offline">离线</option>
            <option value="unknown">未连通</option>
          </select>
          <label className="row" style={{ gap: 6, fontSize: 13, color: 'var(--ink-2)', whiteSpace: 'nowrap' }}>
            <input type="checkbox" checked={hasIdle} onChange={(e) => setHasIdle(e.target.checked)} />只看有空闲卡
          </label>
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          <span className="lbl" style={{ marginRight: 4 }}>分组</span>
          <Chips value={by} options={GROUP_BY} onChange={setBy} />
          <span className="grow" />
          <span className="lbl">共 {shown.size} 台匹配</span>
          {hasFilter && <button type="button" className="btn sm" onClick={clear}>清除筛选</button>}
        </div>
      </section>
      <ErrorNote error={grouped.error} />

      <div className="split">
        <div className="col" style={{ flex: '3 1 560px', minWidth: 0, gap: 22 }}>
          {!grouped.loading && shown.size === 0 && <div className="card empty">{allList.length ? '没有符合条件的服务器' : '还没有服务器，点右上角“添加服务器”'}</div>}
          {groups.map((g) => {
            const ss = g.servers
            const name = g.key === null ? '未设置' : (KEY_NAME[by]?.(g.key) ?? g.key)
            return (
              <section key={String(g.key)} className="col" style={{ gap: 10 }}>
                {by !== 'none' && (
                  <div className="row" style={{ alignItems: 'baseline', gap: 10, padding: '0 2px' }}>
                    <h2>{name}</h2>
                    <span className="lbl">
                      {ss.length} 台 · 在线 {ss.filter((s) => s.status === 'online').length} · 可调度空闲 {ss.reduce((a, s) => a + schedulableIdle(s), 0)} / {ss.reduce((a, s) => a + s.device_count, 0)} 卡
                    </span>
                  </div>
                )}
                <div className="srv-grid">
                  {ss.map((s) => <ServerCard key={s.id} s={s} selected={s.id === selId} onClick={() => select(s.id)} />)}
                </div>
              </section>
            )
          })}
        </div>
        {selId ? <ServerDetail key={selId} id={selId} groups={fo?.groups ?? []} onChanged={() => { grouped.reload(); all.reload(); filters.reload() }} onDeleted={() => { setParams({}, { replace: true }); grouped.reload(); all.reload() }} /> : null}
      </div>

      {showAdd && <AddServerDialog groups={fo?.groups ?? []} onClose={() => setShowAdd(false)} onAdded={(s) => { grouped.reload(); all.reload(); filters.reload(); if (s) { setShowAdd(false); select(s.id) } }} />}
    </main>
  )
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <select className="inp" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">全部{label}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

function ServerCard({ s, selected, onClick }: { s: Server; selected: boolean; onClick: () => void }) {
  const acc = s.accelerator ? ACC_LOOK[s.accelerator] : null
  const h = s.host_info
  const memPct = h ? pct(h.memory_used_mb, h.memory_total_mb) : null
  const disk = h?.disks[0]
  return (
    <button type="button" className={selected ? 'srv sel' : 'srv'} onClick={onClick} aria-pressed={selected}>
      <div className="row" style={{ width: '100%' }}>
        <span className="dot" style={{ background: STATUS_LOOK[s.status].dot }} title={STATUS_LOOK[s.status].name} />
        <span className="mono grow ellipsis" style={{ fontWeight: 500 }}>{s.name}</span>
        {acc && <Badge bg={acc.bg} fg={acc.fg}>{acc.name}</Badge>}
      </div>
      <div className="lbl" style={{ marginTop: -6 }}>
        {!s.accelerator ? '连通后自动识别加速卡' : `${s.models.join(' / ') || '无加速卡'} · ${s.device_count} 卡`} · <span className="mono">{s.host}:{s.port}</span>
      </div>
      <div className="row wrap" style={{ gap: 6 }}>
        {s.owner && <span style={{ fontSize: 13, color: 'var(--ink-2)', marginRight: 2 }}>{s.owner}</span>}
        {s.group && <span className="tag">{s.group}</span>}
        {s.tags.map((t) => <span key={t} className="tag outline">{t}</span>)}
        {!s.schedulable && <Badge bg="#FFF4D6" fg="#7A5300">不参与调度</Badge>}
        <DeployBadge s={s} />
      </div>
      {s.devices.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 5, width: '100%' }}>
          {s.devices.map((d) => {
            const look = DEVICE_LOOK[deviceState(s, d)]
            return (
              <div key={d.index} className="kc" title={`卡 ${d.index} · ${look.name} · 显存 ${gb(d.memory_used_mb)}/${gb(d.memory_total_mb)} GB`} style={{ background: look.bg, borderColor: look.bd }}>
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <span className="mono" style={{ fontSize: 11, color: look.fg }}>{d.index}</span>
                  <span className="mono" style={{ fontSize: 12, fontWeight: 500, color: look.fg }}>{num(d.utilization, 0, '%')}</span>
                </div>
                <div style={{ height: 4, borderRadius: 2, background: '#FFFFFF', overflow: 'hidden' }}>
                  <div style={{ height: 4, width: `${pct(d.memory_used_mb, d.memory_total_mb)}%`, background: look.bd }} />
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="row" style={{ gap: 14, width: '100%' }}>
        <span className="lbl">CPU <span className="mono" style={{ color: 'var(--ink)' }}>{num(h?.cpu_percent, 0, '%')}</span></span>
        <span className="lbl">内存 <span className="mono" style={{ color: 'var(--ink)' }}>{memPct === null ? '-' : `${memPct.toFixed(0)}%`}</span></span>
        <span className="lbl">磁盘 <span className="mono" style={{ color: 'var(--ink)' }}>{disk ? `${pct(disk.used_gb, disk.total_gb).toFixed(0)}%` : '-'}</span></span>
        <span className="grow" />
        <span className="mono" style={{ fontSize: 13, fontWeight: 500, color: s.status === 'online' && s.idle_device_count ? '#1B5E3A' : 'var(--muted)' }}>
          {s.status === 'online' ? `空闲 ${s.idle_device_count}/${s.device_count}` : STATUS_LOOK[s.status].name}
        </span>
      </div>
    </button>
  )
}

function ServerDetail({ id, groups, onChanged, onDeleted }: { id: number; groups: string[]; onChanged: () => void; onDeleted: () => void }) {
  const [tab, setTab] = useState<'cards' | 'trend' | 'agent' | 'attr'>('cards')
  const detail = usePoll(() => serversApi.get(id), [id])
  const [refreshing, setRefreshing] = useState(false)
  const s = detail.data
  if (!s) return <aside className="card" style={{ flex: '2 1 400px', minWidth: 0 }}><div className="empty">{detail.error ?? '加载中…'}</div></aside>
  const acc = s.accelerator ? ACC_LOOK[s.accelerator] : null
  const h = s.host_info
  const disk = h?.disks[0]
  const refresh = async () => {
    setRefreshing(true)
    try {
      detail.setData(await serversApi.refresh(id))
      onChanged()
    } finally {
      setRefreshing(false)
    }
  }
  const hostBars = [
    { k: `CPU${h?.cpu_count ? ` · ${h.cpu_count} 核` : ''}`, v: num(h?.cpu_percent, 0, '%'), p: h?.cpu_percent ?? 0, sub: h?.load1 !== null && h?.load1 !== undefined ? `负载 ${h.load1}` : '' },
    { k: `内存${h?.memory_total_mb ? ` · ${gb(h.memory_total_mb, 0)} GB` : ''}`, v: h ? `${pct(h.memory_used_mb, h.memory_total_mb).toFixed(0)}%` : '-', p: h ? pct(h.memory_used_mb, h.memory_total_mb) : 0, sub: '' },
    { k: `磁盘 ${disk?.path ?? ''}`, v: disk ? `${pct(disk.used_gb, disk.total_gb).toFixed(0)}%` : '-', p: disk ? pct(disk.used_gb, disk.total_gb) : 0, sub: disk ? `${num(disk.used_gb)} / ${num(disk.total_gb)} GB` : '' },
  ]

  return (
    <aside className="card col" style={{ flex: '2 1 400px', minWidth: 0, position: 'sticky', top: 16 }}>
      <div className="col" style={{ padding: '18px 18px 0', gap: 6 }}>
        <div className="row">
          <span className="dot" style={{ width: 10, height: 10, borderRadius: 5, background: STATUS_LOOK[s.status].dot }} />
          <h2 className="mono grow ellipsis" style={{ fontSize: 18, fontWeight: 500 }}>{s.name}</h2>
          {acc && <Badge bg={acc.bg} fg={acc.fg}>{acc.name} · {s.models.join(' / ')}</Badge>}
        </div>
        <div className="row">
          <div className="lbl grow">
            <span className="mono">{s.host}:{s.port}</span> · 主机名 <span className="mono">{s.hostname ?? '-'}</span> · Agent {s.agent_version ?? '-'} · {s.status === 'online' ? `${relTime(s.last_seen_at)}更新` : `${STATUS_LOOK[s.status].name}${s.last_seen_at ? ` · 最后在线 ${relTime(s.last_seen_at)}` : ''}`}
          </div>
          <button type="button" className="btn sm" onClick={refresh} disabled={refreshing}><IconRefresh />{refreshing ? '刷新中' : '立即刷新'}</button>
        </div>
        {s.last_error && s.status !== 'online' && <div className="notice err">最后错误：<span className="mono">{s.last_error}</span></div>}
        {s.deploy?.status === 'failed' && <div className="notice err">Agent {s.deploy.action === 'upgrade' ? '升级' : '安装'}失败：{s.deploy.error}</div>}
        <div className="row" style={{ gap: 20, marginTop: 4, alignItems: 'flex-start' }}>
          {hostBars.map((b) => (
            <div key={b.k} className="col" style={{ flex: '1 1 0', gap: 4, minWidth: 0 }}>
              <div className="row" style={{ justifyContent: 'space-between' }}><span className="lbl ellipsis">{b.k}</span><span className="mono" style={{ fontSize: 12 }}>{b.v}</span></div>
              <div className="bar" style={{ height: 4 }}><div style={{ width: `${b.p}%`, background: 'var(--ink-2)' }} /></div>
              {b.sub && <span className="lbl" style={{ fontSize: 11 }}>{b.sub}</span>}
            </div>
          ))}
        </div>
        <div className="row" style={{ gap: 20, marginTop: 8, borderBottom: '1px solid var(--border)' }} role="tablist">
          {([['cards', `加速卡 ${s.device_count}`], ['trend', '趋势'], ['agent', 'Agent'], ['attr', '属性']] as const).map(([k, n]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? 'tab on' : 'tab'} onClick={() => setTab(k)}>{n}</button>
          ))}
        </div>
      </div>
      {tab === 'cards' && (
        <div className="col">
          {s.devices.length === 0 && <div className="empty">{s.status === 'unknown' ? '尚未连通，连通后自动识别加速卡' : '没有加速卡数据'}</div>}
          {s.devices.map((d) => {
            const look = DEVICE_LOOK[deviceState(s, d)]
            return (
              <div key={d.index} className="col" style={{ padding: '12px 18px', borderBottom: '1px solid var(--border-soft)', gap: 6 }}>
                <div className="row wrap">
                  <span className="mono" style={{ fontWeight: 500, width: 40 }}>卡 {d.index}</span>
                  <Badge bg={look.bg} fg={look.fg}>{d.job_id ? `平台任务 #${d.job_id}` : look.name}</Badge>
                  {d.health && d.health.toUpperCase() !== 'OK' && <span className="lbl">健康：{d.health}</span>}
                  <span className="grow" />
                  <span className="mono lbl">利用率 <span style={{ color: 'var(--ink)' }}>{num(d.utilization, 0, '%')}</span></span>
                  <span className="mono lbl">{num(d.temperature, 0, '°C')}</span>
                  <span className="mono lbl">{num(d.power_w, 0, 'W')}</span>
                </div>
                <div className="row" style={{ gap: 10 }}>
                  <div className="bar grow"><div style={{ width: `${pct(d.memory_used_mb, d.memory_total_mb)}%`, background: look.bd }} /></div>
                  <span className="mono" style={{ fontSize: 12, width: 120, textAlign: 'right' }}>{gb(d.memory_used_mb)} / {gb(d.memory_total_mb, 0)} GB</span>
                </div>
                <div className="lbl mono">
                  {d.bus_id ?? '-'} · {d.processes.length ? d.processes.map((p) => `PID ${p.pid ?? '-'} · ${p.user ?? '-'} · ${p.name ?? ''} ${gb(p.memory_mb)} GB`).join('；') : '无进程'}
                </div>
              </div>
            )
          })}
        </div>
      )}
      {tab === 'trend' && <Trend server={s} />}
      {tab === 'agent' && <AgentPanel s={s} onChanged={(n) => { if (n) detail.setData(n); else detail.reload(); onChanged() }} />}
      {tab === 'attr' && <AttrForm s={s} groups={groups} onSaved={(n) => { detail.setData(n); onChanged() }} onDeleted={onDeleted} />}
    </aside>
  )
}

const DEPLOY_LOOK = {
  pending: { name: '等待安装', bg: '#E8EEFB', fg: '#1D4AA8' },
  running: { name: '安装中', bg: '#E8EEFB', fg: '#1D4AA8' },
  failed: { name: '安装失败', bg: '#FBE1DF', fg: '#A1281F' },
} as const

function DeployBadge({ s }: { s: Server }) {
  const d = s.deploy
  if (d && d.status !== 'succeeded') {
    const look = DEPLOY_LOOK[d.status]
    const name = d.action === 'upgrade' ? look.name.replace('安装', '升级') : look.name
    return <span title={d.error ?? undefined}><Badge bg={look.bg} fg={look.fg}>{name}</Badge></span>
  }
  if (s.managed && s.agent_outdated) return <Badge bg="#FFF4D6" fg="#7A5300">Agent 待升级</Badge>
  return null
}

function AgentPanel({ s, onChanged }: { s: Server; onChanged: (s: Server | null) => void }) {
  const active = s.deploy?.status === 'pending' || s.deploy?.status === 'running'
  const detail = usePoll(() => serversApi.deployDetail(s.id), [s.id, active], active ? 2_000 : 0)
  const pkg = usePoll(() => serversApi.agentPackage(), [], 0)
  const [err, setErr] = useState<string | null>(null)
  const [showLog, setShowLog] = useState(false)
  const d = detail.data
  // 部署在后台结束后刷新服务器详情
  useEffect(() => {
    if (active && d && d.status !== 'pending' && d.status !== 'running') onChanged(null)
  }, [active, d, onChanged])
  const run = async () => {
    setErr(null)
    try {
      onChanged(await serversApi.deploy(s.id))
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    }
  }
  const verb = s.agent_version ? (s.agent_outdated ? '升级 Agent' : '重新安装 Agent') : '安装 Agent'
  const rows: [string, ReactNode][] = [
    ['当前版本', <span className="mono">{s.agent_version ?? '未连通'}</span>],
    ['中心服务自带版本', <span className="mono">{pkg.data?.version ?? '-'}</span>],
    ['管理方式', s.managed ? <span>中心服务通过 SSH 安装和升级（<span className="mono">{s.ssh_user}@{s.ssh_host ?? s.host}:{s.ssh_port}</span>）</span> : '手动安装'],
    ['允许读取的目录', <span className="mono">{s.allow_roots.length ? s.allow_roots.join('、') : '登录用户家目录'}</span>],
  ]
  return (
    <div className="col" style={{ padding: '16px 18px', gap: 12 }}>
      {rows.map(([k, v]) => (
        <div key={k} className="row" style={{ gap: 12, alignItems: 'baseline' }}>
          <span className="lbl" style={{ width: 110, flex: 'none' }}>{k}</span>
          <span style={{ fontSize: 13, minWidth: 0, wordBreak: 'break-all' }}>{v}</span>
        </div>
      ))}
      {d && (
        <div className={d.status === 'failed' ? 'notice err' : d.status === 'succeeded' ? 'notice ok' : 'notice'}>
          最近一次{d.action === 'upgrade' ? '升级' : '安装'}：{{ pending: '等待中', running: '执行中', succeeded: '成功', failed: '失败' }[d.status]}
          {d.finished_at ? ` · ${relTime(d.finished_at)}` : ''}{d.error ? `：${d.error}` : ''}
        </div>
      )}
      {!s.managed && <div className="notice">在“属性”里填写 SSH 用户后，就可以由中心服务安装和升级 Agent。</div>}
      {err && <div className="notice err">{err}</div>}
      <div className="row" style={{ gap: 8 }}>
        <button type="button" className="btn pri sm" onClick={run} disabled={!s.managed || active}>{active ? '执行中…' : verb}</button>
        {d?.log && <button type="button" className="btn sm" onClick={() => setShowLog(!showLog)}>{showLog ? '收起输出' : '查看输出'}</button>}
        {pkg.data?.auto_upgrade && s.managed && <span className="lbl">中心服务升级后会自动升级托管的 Agent</span>}
      </div>
      {showLog && d?.log && <pre className="log">{d.log}</pre>}
    </div>
  )
}

function Trend({ server }: { server: Server }) {
  const [hours, setHours] = useState<1 | 24 | 168>(24)
  const [dev, setDev] = useState<'all' | number>('all')
  const hist = usePoll(() => serversApi.history(server.id, hours), [server.id, hours], 60_000)
  const totals = new Map(server.devices.map((d) => [d.index, d.memory_total_mb]))
  // 按时间点合并：全部卡取平均，单卡直接取值
  const byTs = new Map<string, { u: number[]; m: number[] }>()
  for (const h of hist.data ?? []) {
    if (dev !== 'all' && h.device_index !== dev) continue
    for (const p of h.points) {
      const e = byTs.get(p.ts) ?? { u: [], m: [] }
      if (p.utilization !== null) e.u.push(p.utilization)
      const total = totals.get(h.device_index)
      if (p.memory_used_mb !== null && total) e.m.push((p.memory_used_mb / total) * 100)
      byTs.set(p.ts, e)
    }
  }
  const pts = [...byTs.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  const step = Math.max(1, Math.ceil(pts.length / 300))
  const sampled = pts.filter((_, i) => i % step === 0)
  const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null)
  const line = (pick: (e: { u: number[]; m: number[] }) => number[]) =>
    sampled
      .map(([, e], i) => {
        const v = avg(pick(e))
        return v === null ? null : `${(34 + (i * 380) / Math.max(1, sampled.length - 1)).toFixed(1)},${(170 - (v / 100) * 150).toFixed(1)}`
      })
      .filter(Boolean)
      .join(' ')

  return (
    <div className="col" style={{ padding: '16px 18px', gap: 12 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        <Chips value={String(hours) as '1' | '24' | '168'} options={[['1', '1 小时'], ['24', '24 小时'], ['168', '7 天']]} onChange={(v) => setHours(Number(v) as 1 | 24 | 168)} />
        <span className="grow" />
        <select className="inp" aria-label="选择加速卡" style={{ height: 32 }} value={String(dev)} onChange={(e) => setDev(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
          <option value="all">全部卡平均</option>
          {server.devices.map((d) => <option key={d.index} value={d.index}>卡 {d.index}</option>)}
        </select>
      </div>
      <div className="row" style={{ gap: 14, fontSize: 12 }}>
        <span className="row" style={{ gap: 6 }}><span style={{ width: 14, height: 2, background: 'var(--accent)' }} />利用率</span>
        <span className="row" style={{ gap: 6 }}><span style={{ width: 14, height: 2, background: '#C46A0B' }} />显存占用</span>
      </div>
      {sampled.length < 2 ? (
        <div className="empty">{hist.loading ? '加载中…' : '这段时间还没有数据'}</div>
      ) : (
        <svg viewBox="0 0 420 200" width="100%" role="img" aria-label="利用率与显存趋势" style={{ display: 'block' }}>
          {[20, 70, 120].map((y) => <line key={y} x1="34" y1={y} x2="414" y2={y} stroke="#EEF0EC" />)}
          <line x1="34" y1="170" x2="414" y2="170" stroke="#CBD0CA" />
          {[['100%', 24], ['67%', 74], ['33%', 124], ['0', 174]].map(([t, y]) => <text key={t} x="28" y={y} textAnchor="end" fontSize="10" fill="#5E646D">{t}</text>)}
          <polyline points={line((e) => e.m)} fill="none" stroke="#C46A0B" strokeWidth="1.5" strokeLinejoin="round" />
          <polyline points={line((e) => e.u)} fill="none" stroke="#1D5BD6" strokeWidth="1.5" strokeLinejoin="round" />
          <text x="34" y="190" fontSize="10" fill="#5E646D">{hours === 168 ? '7 天前' : `${hours} 小时前`}</text>
          <text x="414" y="190" textAnchor="end" fontSize="10" fill="#5E646D">现在</text>
        </svg>
      )}
      <div className="lbl">历史每分钟一个点，保留 7 天；点数较多时按时间均匀抽样显示。</div>
    </div>
  )
}

function AttrForm({ s, groups, onSaved, onDeleted }: { s: Server; groups: string[]; onSaved: (s: Server) => void; onDeleted: () => void }) {
  const [name, setName] = useState(s.name)
  const [group, setGroup] = useState(s.group ?? '')
  const [owner, setOwner] = useState(s.owner ?? '')
  const [tags, setTags] = useState<string[]>(s.tags)
  const [tagInput, setTagInput] = useState('')
  const [note, setNote] = useState(s.note ?? '')
  const [schedulable, setSchedulable] = useState(s.schedulable)
  const [sshUser, setSshUser] = useState(s.ssh_user ?? '')
  const [sshPort, setSshPort] = useState(String(s.ssh_port))
  const [sshHost, setSshHost] = useState(s.ssh_host ?? '')
  const [port, setPort] = useState(String(s.port))
  const [roots, setRoots] = useState(s.allow_roots.join('\n'))
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState(false)
  const addTag = () => {
    const t = tagInput.trim()
    if (t && !tags.includes(t)) setTags([...tags, t])
    setTagInput('')
  }
  const save = async () => {
    setErr(null)
    setOk(false)
    try {
      onSaved(await serversApi.update(s.id, {
        name, group: group || null, owner: owner || null, tags, note: note || null, schedulable,
        ssh_user: sshUser.trim() || null, ssh_port: Number(sshPort) || 22, ssh_host: sshHost.trim() || null, port: Number(port) || 9100,
        allow_roots: roots.split('\n').map((r) => r.trim()).filter(Boolean),
      }))
      setOk(true)
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    }
  }
  const remove = async () => {
    if (!window.confirm(`删除服务器 ${s.name}？它的历史数据也会一并删除。`)) return
    try {
      await serversApi.remove(s.id)
      onDeleted()
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    }
  }
  return (
    <form className="col" style={{ padding: '16px 18px', gap: 14 }} onSubmit={(e) => { e.preventDefault(); save() }}>
      <label className="field"><span className="lbl">名称</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} required /></label>
      <div className="row" style={{ gap: 12 }}>
        <label className="field grow"><span className="lbl">运行组</span>
          <input className="inp" list="group-options" value={group} onChange={(e) => setGroup(e.target.value)} placeholder="例如 训练组" />
          <datalist id="group-options">{groups.map((g) => <option key={g} value={g} />)}</datalist>
        </label>
        <label className="field grow"><span className="lbl">使用人</span><input className="inp" value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
      </div>
      <div className="field">
        <span className="lbl">标签</span>
        <div className="row wrap" style={{ gap: 6, minHeight: 36, border: '1px solid var(--control)', borderRadius: 6, padding: '4px 8px' }}>
          {tags.map((t) => (
            <span key={t} className="tag">{t}
              <button type="button" aria-label={`移除标签 ${t}`} onClick={() => setTags(tags.filter((x) => x !== t))} style={{ border: 0, background: 'none', cursor: 'pointer', color: 'var(--faint)', padding: '0 0 0 4px' }}>×</button>
            </span>
          ))}
          <input aria-label="添加标签" placeholder="输入后回车添加" value={tagInput} onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }} onBlur={addTag}
            style={{ border: 0, outline: 'none', font: 'inherit', fontSize: 13, flexGrow: 1, minWidth: 100 }} />
        </div>
      </div>
      <label className="field"><span className="lbl">备注</span><textarea className="inp" rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></label>
      <div className="row" style={{ gap: 12 }}>
        <label className="field" style={{ flex: '3 1 0' }}><span className="lbl">SSH 用户（不填为手动安装）</span><input className="inp mono" value={sshUser} onChange={(e) => setSshUser(e.target.value)} /></label>
        <label className="field" style={{ flex: '1 1 0' }}><span className="lbl">SSH 端口</span><input className="inp mono" value={sshPort} onChange={(e) => setSshPort(e.target.value)} inputMode="numeric" /></label>
        <label className="field" style={{ flex: '1 1 0' }}><span className="lbl">Agent 端口</span><input className="inp mono" value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" /></label>
      </div>
      <label className="field"><span className="lbl">SSH 连接目标（可填 ~/.ssh/config 中的别名，不填用地址）</span><input className="inp mono" value={sshHost} onChange={(e) => setSshHost(e.target.value)} placeholder={s.host} /></label>
      <label className="field"><span className="lbl">允许读取的目录（每行一个，修改后需重新安装 Agent 生效）</span><textarea className="inp mono" rows={2} value={roots} onChange={(e) => setRoots(e.target.value)} placeholder="登录用户家目录" /></label>
      <div className="row" style={{ gap: 12 }}>
        <div className="grow">
          <div style={{ fontWeight: 500 }}>参与调度</div>
          <div className="lbl">关闭后调度器不会再往这台服务器分配任务，已运行的任务不受影响</div>
        </div>
        <Switch on={schedulable} onChange={setSchedulable} label="参与调度" />
      </div>
      {err && <div className="notice err">{err}</div>}
      {ok && <div className="notice ok">已保存</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button type="button" className="btn danger" style={{ marginRight: 'auto' }} onClick={remove}>删除服务器</button>
        <button type="submit" className="btn pri">保存</button>
      </div>
    </form>
  )
}

type Parsed = { line: number; host: string; ssh_user: string | null; ssh_port: number; name: string | null } | { line: number; error: string }

/** 批量添加的一行：[用户@]地址[:SSH端口] [名称] */
function parseLine(text: string, line: number, defaultUser: string): Parsed | null {
  const t = text.trim()
  if (!t || t.startsWith('#')) return null
  const [target, name, ...rest] = t.split(/\s+/)
  if (rest.length) return { line, error: '格式应为 [用户@]地址[:SSH端口] [名称]' }
  const m = /^(?:([^@\s]+)@)?([^:@\s]+)(?::(\d+))?$/.exec(target)
  if (!m) return { line, error: `无法识别 ${target}` }
  const port = m[3] ? Number(m[3]) : 22
  if (port < 1 || port > 65535) return { line, error: `SSH 端口 ${m[3]} 不合法` }
  return { line, host: m[2], ssh_user: m[1] || defaultUser.trim() || null, ssh_port: port, name: name ?? null }
}

function AddServerDialog({ groups, onClose, onAdded }: { groups: string[]; onClose: () => void; onAdded: (s: Server | null) => void }) {
  const [mode, setMode] = useState<'one' | 'batch' | 'import'>('one')
  const sshConfig = usePoll(() => serversApi.sshConfig(), [mode === 'import'], 0)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const importable = (sshConfig.data?.hosts ?? []).filter((h) => !h.added)
  const togglePick = (alias: string) => setPicked((prev) => {
    const next = new Set(prev)
    if (next.has(alias)) next.delete(alias)
    else next.add(alias)
    return next
  })
  const [host, setHost] = useState('')
  const [name, setName] = useState('')
  const [sshUser, setSshUser] = useState('')
  const [sshPort, setSshPort] = useState('22')
  const [lines, setLines] = useState('')
  const [port, setPort] = useState('9100')
  const [roots, setRoots] = useState('')
  const [group, setGroup] = useState('')
  const [owner, setOwner] = useState('')
  const [deploy, setDeploy] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [failed, setFailed] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const pkg = usePoll(() => serversApi.agentPackage(), [], 0)

  const parsed = useMemo(
    () => lines.split('\n').map((l, i) => parseLine(l, i + 1, sshUser)).filter((p): p is Parsed => p !== null),
    [lines, sshUser],
  )
  const bad = parsed.filter((p): p is Extract<Parsed, { error: string }> => 'error' in p)
  const good = parsed.filter((p): p is Extract<Parsed, { host: string }> => 'host' in p)
  const anySsh = mode === 'one' ? !!sshUser.trim() : mode === 'batch' ? good.some((p) => p.ssh_user) : picked.size > 0

  const submit = async () => {
    setErr(null)
    setFailed([])
    setBusy(true)
    const common = {
      port: Number(port) || 9100,
      group: group || null,
      owner: owner || null,
      allow_roots: roots.split('\n').map((r) => r.trim()).filter(Boolean),
    }
    const items = mode === 'one'
      ? [{ ...common, host: host.trim(), name: name.trim() || null, ssh_user: sshUser.trim() || null, ssh_port: Number(sshPort) || 22 }]
      : mode === 'batch'
        ? good.map((p) => ({ ...common, host: p.host, name: p.name, ssh_user: p.ssh_user, ssh_port: p.ssh_port }))
        // 以别名作为 SSH 连接目标，沿用 config 里的密钥、跳板机等设置
        : importable.filter((h) => picked.has(h.alias)).map((h) => ({ ...common, host: h.hostname, name: h.alias, ssh_host: h.alias, ssh_user: h.user, ssh_port: h.port }))
    try {
      const res = await serversApi.batchCreate(items, deploy)
      if (res.errors.length) {
        setFailed(res.errors.map((e) => `${e.host}：${e.error}`))
        if (res.created.length) onAdded(null)
      } else {
        const one = res.created.length === 1 ? res.created[0] : null
        onAdded(one)
        if (!one) onClose()
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setBusy(false)
    }
  }
  const p = pkg.data
  return (
    <Modal title="添加服务器" onClose={onClose} width={560}>
      <form className="col" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); submit() }}>
        <div className="row" style={{ gap: 6 }}>
          <Chips value={mode} options={[['one', '单台'], ['batch', '批量'], ['import', '从 SSH 配置导入']]} onChange={setMode} />
        </div>
        {mode === 'one' ? (
          <>
            <div className="row" style={{ gap: 12 }}>
              <label className="field" style={{ flex: '3 1 0' }}><span className="lbl">地址</span><input className="inp mono" placeholder="10.0.1.41" value={host} onChange={(e) => setHost(e.target.value)} required autoFocus /></label>
              <label className="field" style={{ flex: '2 1 0' }}><span className="lbl">名称（不填则用地址）</span><input className="inp" value={name} onChange={(e) => setName(e.target.value)} /></label>
            </div>
            <div className="row" style={{ gap: 12 }}>
              <label className="field" style={{ flex: '3 1 0' }}><span className="lbl">SSH 用户（不填表示已手动安装 Agent）</span><input className="inp mono" placeholder="alice" value={sshUser} onChange={(e) => setSshUser(e.target.value)} /></label>
              <label className="field" style={{ flex: '1 1 0' }}><span className="lbl">SSH 端口</span><input className="inp mono" value={sshPort} onChange={(e) => setSshPort(e.target.value)} inputMode="numeric" /></label>
            </div>
          </>
        ) : mode === 'import' ? (
          <div className="col" style={{ gap: 8 }}>
            <div className="row">
              <span className="lbl grow">读取中心主机的 <span className="mono">{sshConfig.data?.path ?? '~/.ssh/config'}</span>，名称和 SSH 连接目标用 Host 别名</span>
              {importable.length > 0 && (
                <button type="button" className="btn sm" onClick={() => setPicked(picked.size === importable.length ? new Set() : new Set(importable.map((h) => h.alias)))}>
                  {picked.size === importable.length ? '全不选' : '全选'}
                </button>
              )}
            </div>
            {sshConfig.error && <div className="notice err">{sshConfig.error}</div>}
            {sshConfig.data && sshConfig.data.hosts.length === 0 && <div className="notice">没有读到主机（不含通配符的 Host 条目）。</div>}
            <div className="col" style={{ maxHeight: 240, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 6 }}>
              {(sshConfig.data?.hosts ?? []).map((h) => (
                <label key={h.alias} className="row" style={{ gap: 8, padding: '6px 10px', borderBottom: '1px solid var(--border-soft)', fontSize: 13, opacity: h.added ? 0.5 : 1 }}>
                  <input type="checkbox" disabled={h.added} checked={picked.has(h.alias)} onChange={() => togglePick(h.alias)} />
                  <span className="mono" style={{ width: 140 }}>{h.alias}</span>
                  <span className="mono grow lbl">{h.user}@{h.hostname}{h.port !== 22 ? `:${h.port}` : ''}</span>
                  {h.added && <span className="lbl">已添加</span>}
                </label>
              ))}
            </div>
          </div>
        ) : (
          <>
            <label className="field">
              <span className="lbl">每行一台：[用户@]地址[:SSH端口] [名称]，# 开头的行忽略</span>
              <textarea className="inp mono" rows={7} value={lines} onChange={(e) => setLines(e.target.value)} autoFocus
                placeholder={'alice@10.0.1.41\n10.0.1.42 gpu-02\nbob@10.0.2.11:2222 npu-01'} />
            </label>
            <label className="field"><span className="lbl">默认 SSH 用户（行里没写用户时使用）</span><input className="inp mono" value={sshUser} onChange={(e) => setSshUser(e.target.value)} /></label>
            <div className="lbl">
              识别到 {good.length} 台{good.length ? `，其中 ${good.filter((g) => g.ssh_user).length} 台自动安装 Agent` : ''}
              {bad.map((b) => <div key={b.line} style={{ color: '#A1281F' }}>第 {b.line} 行：{b.error}</div>)}
            </div>
          </>
        )}
        <div className="row" style={{ gap: 12 }}>
          <label className="field grow" style={{ minWidth: 0 }}><span className="lbl">运行组</span>
            <input className="inp" list="add-group-options" value={group} onChange={(e) => setGroup(e.target.value)} />
            <datalist id="add-group-options">{groups.map((g) => <option key={g} value={g} />)}</datalist>
          </label>
          <label className="field grow" style={{ minWidth: 0 }}><span className="lbl">使用人</span><input className="inp" value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
          <label className="field" style={{ flex: '0 0 100px', minWidth: 0 }}><span className="lbl">Agent 端口</span><input className="inp mono" style={{ width: '100%' }} value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" /></label>
        </div>
        {anySsh && (
          <>
            <label className="field">
              <span className="lbl">允许读取的目录（推理结果所在位置，每行一个，不填为登录用户家目录）</span>
              <textarea className="inp mono" rows={2} value={roots} onChange={(e) => setRoots(e.target.value)} placeholder="/data/results" />
            </label>
            <label className="row" style={{ gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={deploy} onChange={(e) => setDeploy(e.target.checked)} />添加后立即安装 Agent
            </label>
            {p && !p.ssh_available && <div className="notice err">中心主机上找不到 ssh 命令，无法自动安装。</div>}
            <div className="notice">
              中心主机通过 SSH 密钥免密登录，把 Agent 装到登录用户的 <span className="mono">~/.gnm-agent</span>，任务以该用户运行，不需要 root。
              {p?.public_key ? (
                <>请先把中心主机的公钥加入各服务器该用户的 <span className="mono">~/.ssh/authorized_keys</span>：
                  <textarea className="inp mono" readOnly rows={2} value={p.public_key} onFocus={(e) => e.currentTarget.select()} style={{ marginTop: 6, width: '100%', fontSize: 11 }} />
                </>
              ) : p ? <>中心主机还没有 SSH 密钥，请先用 <span className="mono">ssh-keygen</span> 生成，再用 <span className="mono">ssh-copy-id</span> 分发到各服务器。</> : null}
            </div>
          </>
        )}
        {!anySsh && <div className="notice">不填 SSH 用户时需要先在服务器上手动安装 Agent，添加后会立即尝试连接。</div>}
        {err && <div className="notice err">{err}</div>}
        {failed.length > 0 && <div className="notice err">以下服务器没有添加：{failed.map((f) => <div key={f}>{f}</div>)}</div>}
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>{failed.length ? '关闭' : '取消'}</button>
          <button type="submit" className="btn pri" disabled={busy || (mode === 'one' ? !host.trim() : mode === 'batch' ? !good.length || bad.length > 0 : !picked.size)}>
            {busy ? '添加中' : mode === 'one' ? '添加' : `添加 ${mode === 'batch' ? good.length : picked.size} 台`}
          </button>
        </div>
      </form>
    </Modal>
  )
}
