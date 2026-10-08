export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail)
  }
}

type Query = Record<string, string | number | boolean | string[] | null | undefined>

function buildQuery(query?: Query): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    // 多值参数用重复参数传递，例如 ?tag=a&tag=b
    if (Array.isArray(value)) value.forEach((v) => params.append(key, v))
    else params.append(key, String(value))
  }
  const s = params.toString()
  return s ? `?${s}` : ''
}

export async function request<T>(method: string, path: string, opts: { query?: Query; body?: unknown } = {}): Promise<T> {
  const resp = await fetch(`/api${path}${buildQuery(opts.query)}`, {
    method,
    headers: opts.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const data = await resp.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // 非 JSON 错误体，保留状态文本
    }
    throw new ApiError(resp.status, detail)
  }
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

// 任务、结果等接口在后端第 2、3 步上线前不存在，FastAPI 对未注册路由返回 404 "Not Found"。
// 这时自动改用前端内置的示例数据，页面上会标出“示例数据”，接口上线后无需改动即可切换。
export type Feature = 'jobs' | 'scheduler' | 'results'
const missing = new Set<Feature>()
const listeners = new Set<() => void>()

export function usingMock(feature: Feature): boolean {
  return missing.has(feature)
}

export function onMockChange(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export async function withFallback<T>(feature: Feature, real: () => Promise<T>, mock: () => T | Promise<T>): Promise<T> {
  if (missing.has(feature)) return mock()
  try {
    return await real()
  } catch (e) {
    if (e instanceof ApiError && e.status === 404 && e.detail === 'Not Found') {
      missing.add(feature)
      listeners.forEach((fn) => fn())
      return mock()
    }
    throw e
  }
}
