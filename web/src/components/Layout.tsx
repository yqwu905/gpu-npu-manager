import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { IconChart, IconGrid, IconImage, IconList, IconServer } from './Icons'
import { jobsApi } from '../api/jobs'
import { useMock, usePoll } from '../lib/hooks'

const LINKS = [
  { to: '/', name: '总览', icon: <IconGrid /> },
  { to: '/servers', name: '服务器', icon: <IconServer /> },
  { to: '/jobs', name: '任务队列', icon: <IconList /> },
  { to: '/results', name: '结果与评测', icon: <IconImage /> },
  { to: '/compare', name: '指标对比', icon: <IconChart /> },
]

export default function Layout({ children }: { children: ReactNode }) {
  const sched = usePoll(() => jobsApi.scheduler(), [], 30_000)
  const schedMock = useMock('scheduler')
  return (
    <div className="app">
      <nav className="side" aria-label="主导航">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><span /><span /><span /><span /></div>
          算力平台
        </div>
        {LINKS.map((l) => (
          <NavLink key={l.to} to={l.to} end={l.to === '/'} className={({ isActive }) => (isActive ? 'nav on' : 'nav')}>
            {l.icon}
            {l.name}
          </NavLink>
        ))}
        <div className="side-foot">
          <div className="row" style={{ fontSize: 13, fontWeight: 500 }}>
            <span className="dot" style={{ background: '#2E8B57' }} />
            调度器
          </div>
          <div className="lbl">{schedMock ? '每 5 秒扫描队列' : sched.data ? (sched.data.strict_order ? '严格按顺序' : '回填模式') : '加载中'}</div>
        </div>
      </nav>
      {children}
    </div>
  )
}
