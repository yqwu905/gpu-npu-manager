import { useCallback, useEffect, useRef, useState } from 'react'
import { onMockChange, usingMock, type Feature } from '../api/client'

/** 加载数据并按间隔轮询；deps 变化时重新加载 */
export function usePoll<T>(load: () => Promise<T>, deps: unknown[], intervalMs = 10_000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loadRef = useRef(load)
  loadRef.current = load
  const seq = useRef(0)

  const reload = useCallback(async () => {
    const id = ++seq.current
    try {
      const value = await loadRef.current()
      if (id === seq.current) {
        setData(value)
        setError(null)
      }
    } catch (e) {
      if (id === seq.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (id === seq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    reload()
    if (!intervalMs) return
    const t = setInterval(reload, intervalMs)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, reload, setData }
}

export function useMock(feature: Feature): boolean {
  const [mock, setMock] = useState(usingMock(feature))
  useEffect(() => onMockChange(() => setMock(usingMock(feature))), [feature])
  return mock
}
