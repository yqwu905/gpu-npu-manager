import { useEffect, type CSSProperties, type ReactNode } from 'react'
import { IconClose } from './Icons'

export function MockBadge({ show, what }: { show: boolean; what: string }) {
  if (!show) return null
  return (
    <span className="badge" style={{ background: '#FFF4D6', color: '#7A5300' }} title={`${what}接口尚未上线，当前展示内置示例数据，操作不会保存到服务端`}>
      示例数据 · {what}接口未上线
    </span>
  )
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="notice err">加载失败：{error}</div>
}

export function Badge({ bg, fg, children }: { bg: string; fg: string; children: ReactNode }) {
  return <span className="badge" style={{ background: bg, color: fg }}>{children}</span>
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
        <div className="row">
          <h2 className="grow" style={{ fontSize: 18 }}>{title}</h2>
          <button type="button" className="btn sm" aria-label="关闭" onClick={onClose} style={{ border: 0 }}><IconClose /></button>
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
        <button key={k} type="button" className={value === k ? 'chip on' : 'chip'} onClick={() => onChange(k)} aria-pressed={value === k}>
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
