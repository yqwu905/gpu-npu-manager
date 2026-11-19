import { useEffect, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import { IconClose } from './Icons'

export function MockBadge({ show, what }: { show: boolean; what: string }) {
  if (!show) return null
  return (
    <span className="badge" style={{ background: '#FFF4D6', color: '#7A5300', maxWidth: 260 }} title={`${what}接口尚未上线，当前展示内置示例数据，操作不会保存到服务端`}>
      示例数据 · {what}接口未上线
    </span>
  )
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="notice err">加载失败：{error}</div>
}

export function Badge({ bg, fg, title, className, style, children }: { bg: string; fg: string; title?: string; className?: string; style?: CSSProperties; children: ReactNode }) {
  const tip = title ?? (typeof children === 'string' ? children : undefined)
  return (
    <span className={className ? `badge ${className}` : 'badge'} style={{ background: bg, color: fg, ...style }} title={tip}>
      {children}
    </span>
  )
}

/** 标签列表：自动截断超长单标签并提供完整悬停提示；数量过多时折叠为 +N 可展开胶囊 */
export function TagList({
  tags,
  maxVisible = 5,
  outline = false,
  badge = false,
  asSpanToggle = false,
}: {
  tags: string[]
  maxVisible?: number
  outline?: boolean
  badge?: boolean
  /** 父容器本身是 <button> 时使用 span[role=button] 避免非法 button 嵌套 */
  asSpanToggle?: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  if (!tags.length) return null
  const collapsible = tags.length > maxVisible + 1
  const shown = !collapsible || expanded ? tags : tags.slice(0, maxVisible)
  const hidden = collapsible && !expanded ? tags.slice(maxVisible) : []
  const itemClass = badge ? 'badge' : outline ? 'tag outline' : 'tag'
  const itemStyle = badge ? { background: '#ECEDEB', color: '#3A4048' } : undefined
  const toggle = (e: ReactMouseEvent | ReactKeyboardEvent) => {
    e.stopPropagation()
    e.preventDefault()
    setExpanded((v) => !v)
  }
  const tip = hidden.length ? `点击展开其余 ${hidden.length} 个标签：${hidden.join('、')}` : '收起标签'
  const label = expanded ? '收起' : `+${hidden.length}`

  return (
    <>
      {shown.map((t) => (
        <span key={t} className={itemClass} style={itemStyle} title={t}>
          {t}
        </span>
      ))}
      {collapsible && (
        asSpanToggle ? (
          <span
            role="button"
            tabIndex={0}
            className="tag tag-more"
            title={tip}
            onClick={toggle}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') toggle(e) }}
          >
            {label}
          </span>
        ) : (
          <button type="button" className="tag tag-more" title={tip} onClick={toggle}>
            {label}
          </button>
        )
      )}
    </>
  )
}

export function Switch({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className={on ? 'switch on' : 'switch'} onClick={() => onChange(!on)}>
      <span />
    </button>
  )
}

export function Modal({ title, onClose, children, drawer = false, width }: { title: string; onClose: () => void; children: ReactNode; drawer?: boolean; width?: number }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className={drawer ? 'overlay right' : 'overlay'} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-label={title} className={drawer ? 'drawer' : 'dialog card'} style={width ? { maxWidth: width } : undefined}>
        <div className="row" style={{ gap: 10 }}>
          <h2 className="grow ellipsis" style={{ fontSize: 18 }} title={title}>{title}</h2>
          <button type="button" className="btn ghost sm" aria-label="关闭" onClick={onClose}><IconClose /></button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function Chips<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <>
      {options.map(([k, name]) => (
        <button key={k} type="button" className={value === k ? 'chip on' : 'chip'} onClick={() => onChange(k)} aria-pressed={value === k} title={name}>
          {name}
        </button>
      ))}
    </>
  )
}

/** 结果集里的图片：接口上线后经中心服务代理读取；示例数据模式下画一块示意色块 */
export function SampleImage({ src, seed, mock, blur = 0, label, style }: { src: string; seed: string; mock: boolean; blur?: number; label: string; style?: CSSProperties }) {
  if (!mock) return <img className="thumb" src={src} alt={label} loading="lazy" style={style} />
  const n = Array.from(seed).reduce((a, c) => a * 31 + c.charCodeAt(0), 7) % 360
  return (
    <div className="thumb" role="img" aria-label={`${label}（示意）`} style={style}>
      <div style={{ position: 'absolute', inset: -6, background: `linear-gradient(${n}deg, hsl(${n} 35% 62%), hsl(${(n + 140) % 360} 40% 78%))`, filter: blur ? `blur(${blur}px)` : undefined }} />
    </div>
  )
}
