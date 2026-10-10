// 不用参数属性，前端单测用 Node 的类型擦除直接运行，它不支持这种写法
export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
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

interface Opts { query?: Query; body?: unknown; signal?: AbortSignal; headers?: Record<string, string> }

async function send(method: string, path: string, opts: Opts): Promise<Response> {
  const resp = await fetch(`/api${path}${buildQuery(opts.query)}`, {
    method,
    headers: opts.body === undefined ? opts.headers : { 'Content-Type': 'application/json', ...opts.headers },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
  })
  // 304 只在调用方自己带 If-None-Match 时出现，交给 requestFull 处理
  if (!resp.ok && resp.status !== 304) {
    let detail = resp.statusText
    try {
      const data = await resp.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // 非 JSON 错误体，保留状态文本
    }
    throw new ApiError(resp.status, detail)
  }
  return resp
}

export async function request<T>(method: string, path: string, opts: { query?: Query; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const resp = await send(method, path, opts)
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

/** 同 request，另外返回响应头与状态码；204 和 304（带 If-None-Match 且未变化）时 data 为 null */
export async function requestFull<T>(method: string, path: string, opts: Opts = {}): Promise<{ data: T | null; headers: Headers; status: number }> {
  const resp = await send(method, path, opts)
  const data = resp.status === 204 || resp.status === 304 ? null : ((await resp.json()) as T)
  return { data, headers: resp.headers, status: resp.status }
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
