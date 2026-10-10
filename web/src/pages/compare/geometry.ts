// 对比视图的几何计算（纯函数）：缩放平移、contain 适配、预览档位、无损瓦片金字塔与设备像素对齐
// 坐标约定：原图像素 (u, w) → 格子 CSS 像素 (ox + u·f, oy + w·f) → 视口 CSS 像素 v.x + v.z·(…) → 设备像素 ×dpr

export const TILE = 512
export const PREVIEW_SIZES = [1024, 2048, 3072]
export const MAX_ZOOM = 16
/** native 原图不超过这个大小且像素不超过 FULL_MAX_PX 时，无损层直接用整张原图 */
export const FULL_MAX_BYTES = 12 * 1024 * 1024
export const FULL_MAX_PX = 16_777_216

export interface View { z: number; x: number; y: number }
/** contain 适配：原图像素 → 格子 CSS 像素的比例 f 与留白偏移 */
export interface Fit { f: number; ox: number; oy: number }
export type Rect = [x: number, y: number, w: number, h: number]

// 所有图片共用同一组缩放与平移；平移限制在图片层仍铺满视口的范围内
export const clampView = (z: number, x: number, y: number, w: number, h: number): View =>
  ({ z, x: Math.min(0, Math.max(w - w * z, x)), y: Math.min(0, Math.max(h - h * z, y)) })
export const zoomAt = (v: View, factor: number, cx: number, cy: number, w: number, h: number): View => {
  const z = Math.min(MAX_ZOOM, Math.max(1, v.z * factor))
  const k = z / v.z
  return clampView(z, cx - (cx - v.x) * k, cy - (cy - v.y) * k, w, h)
}

/** 按原图宽高 contain 到格子里；缩略图、预览、原图、瓦片都画进同一个矩形，切换层级时位置不跳 */
export function fit(W: number, H: number, cw: number, ch: number): Fit {
  if (!(W > 0 && H > 0 && cw > 0 && ch > 0)) return { f: 0, ox: cw / 2, oy: ch / 2 }
  const f = Math.min(cw / W, ch / H)
  return { f, ox: (cw - W * f) / 2, oy: (ch - H * f) / 2 }
}

/** 设备像素 / 原图像素 */
export const scaleK = (v: View, ft: Fit, dpr: number) => v.z * ft.f * dpr

/** 原图坐标 → 设备像素（取整，+0 去掉 -0）；同一坐标总得到同一整数，相邻瓦片共用一条边，没有缝 */
export const devX = (v: View, ft: Fit, dpr: number, u: number) => Math.round(dpr * (v.x + v.z * (ft.ox + u * ft.f))) + 0
export const devY = (v: View, ft: Fit, dpr: number, w: number) => Math.round(dpr * (v.y + v.z * (ft.oy + w * ft.f))) + 0
/** 原图矩形 [x0, x1) × [y0, y1) 对齐后的设备像素矩形 */
export function snapRect(v: View, ft: Fit, dpr: number, x0: number, y0: number, x1: number, y1: number): Rect {
  const l = devX(v, ft, dpr, x0), t = devY(v, ft, dpr, y0)
  return [l, t, devX(v, ft, dpr, x1) - l, devY(v, ft, dpr, y1) - t]
}

/** 预览档位：不小于格子长边的设备像素，超过 3072 取 3072 */
export const bucket = (s: number) => PREVIEW_SIZES.find((b) => b >= s) ?? 3072
export const previewSize = (cw: number, ch: number, dpr: number) => bucket(Math.max(cw, ch) * dpr)

/** 与 Pillow Image.thumbnail((s, s)) 相同的输出尺寸；不放大 */
export function previewDims(W: number, H: number, s: number): [number, number] {
  if (W <= s && H <= s) return [W, H]
  const aspect = W / H
  const round = (n: number, key: (k: number) => number) => {
    const a = Math.floor(n), b = Math.ceil(n)
    return Math.max(key(a) <= key(b) ? a : b, 1)
  }
  if (1 >= aspect) return [round(s * aspect, (k) => Math.abs(aspect - k / s)), s]
  return [s, round(s / aspect, (k) => (k === 0 ? 0 : Math.abs(aspect - s / k)))]
}

// 第 l 层 = 原图 reduce(2^l)，尺寸向上取整
export const levelDims = (W: number, H: number, l: number): [number, number] => [Math.ceil(W / 2 ** l), Math.ceil(H / 2 ** l)]
export const maxLevel = (W: number, H: number) => Math.max(0, Math.ceil(Math.log2(Math.max(W, H) / TILE)))
// 屏幕设备像素 / 原图像素 = k；k>=1 用原始像素层，k<1 取分辨率不低于屏幕的无损缩小层
export const levelFor = (k: number, maxL: number) => (k >= 1 ? 0 : Math.min(maxL, Math.floor(Math.log2(1 / k))))
/** 第 l 层横纵各有几块瓦片 */
export function tileCount(W: number, H: number, l: number): [number, number] {
  const [Wl, Hl] = levelDims(W, H, l)
  return [Math.ceil(Wl / TILE), Math.ceil(Hl / TILE)]
}
/** 瓦片 (l, x, y) 覆盖的原图像素范围 [x0, y0, x1, y1)；边缘瓦片截到原图边界 */
export function tileRect(W: number, H: number, l: number, x: number, y: number): [number, number, number, number] {
  const s = TILE * 2 ** l
  return [x * s, y * s, Math.min(W, (x + 1) * s), Math.min(H, (y + 1) * s)]
}

/** 视口（格子 cw×ch CSS 像素）内可见的原图范围 [u0, w0, u1, w1)，已截到图片内；看不到图片时为 null */
export function visibleRect(v: View, ft: Fit, cw: number, ch: number, W: number, H: number): [number, number, number, number] | null {
  if (!(ft.f > 0)) return null
  const u0 = Math.max(0, (-v.x / v.z - ft.ox) / ft.f), u1 = Math.min(W, ((cw - v.x) / v.z - ft.ox) / ft.f)
  const w0 = Math.max(0, (-v.y / v.z - ft.oy) / ft.f), w1 = Math.min(H, ((ch - v.y) / v.z - ft.oy) / ft.f)
  return u1 > u0 && w1 > w0 ? [u0, w0, u1, w1] : null
}

export interface TileKey { x: number; y: number; ring: boolean }
/** 第 l 层与视口相交的瓦片，再加外圈 ring 块（ring: true）；按到视口中心的距离排序，先请求中间的 */
export function visibleTiles(W: number, H: number, l: number, v: View, ft: Fit, cw: number, ch: number, ring = 0): TileKey[] {
  const r = visibleRect(v, ft, cw, ch, W, H)
  if (!r) return []
  const s = TILE * 2 ** l, [nx, ny] = tileCount(W, H, l)
  const tx0 = Math.min(nx - 1, Math.floor(r[0] / s)), tx1 = Math.min(nx - 1, Math.ceil(r[2] / s) - 1)
  const ty0 = Math.min(ny - 1, Math.floor(r[1] / s)), ty1 = Math.min(ny - 1, Math.ceil(r[3] / s) - 1)
  const cx = (r[0] + r[2]) / 2 / s - 0.5, cy = (r[1] + r[3]) / 2 / s - 0.5
  const out: (TileKey & { d: number })[] = []
  for (let y = Math.max(0, ty0 - ring); y <= Math.min(ny - 1, ty1 + ring); y++)
    for (let x = Math.max(0, tx0 - ring); x <= Math.min(nx - 1, tx1 + ring); x++)
      out.push({ x, y, ring: x < tx0 || x > tx1 || y < ty0 || y > ty1, d: (x - cx) ** 2 + (y - cy) ** 2 })
  out.sort((a, b) => Number(a.ring) - Number(b.ring) || a.d - b.d)
  return out.map(({ x, y, ring }) => ({ x, y, ring }))
}

/** 当前缩放下预览是否不够清晰，需要叠加无损层；previewLong 为已画预览的长边像素 */
export const needLossless = (v: View, ft: Fit, dpr: number, W: number, H: number, previewLong: number) =>
  v.z > 1 || Math.max(W, H) * ft.f * v.z * dpr > previewLong * 1.05

export interface LosslessInput {
  W: number
  H: number
  /** 原图字节数，null/NaN 为未知，-1 为文件不存在 */
  size: number | null
  /** 浏览器能否直接显示原图（X-Image-Native） */
  native: boolean
  /** 预览本身已是无损（X-Lossless: 1） */
  previewLossless: boolean
  /** 已画预览的长边像素 */
  previewLong: number
  v: View
  ft: Fit
  cw: number
  ch: number
  dpr: number
  /** 外圈预取几块瓦片，默认 1 */
  ring?: number
}
export type LosslessPlan =
  | { kind: 'none' }
  | { kind: 'full' }
  | { kind: 'tiles'; level: number; tiles: TileKey[] }

/**
 * 无损层取什么：预览已无损或不需要 → none；小的 native 原图且缩小不超过 2 倍 → full；其余 → 当前层级的可见瓦片加一圈
 * （瓦片层级让缩小始终不超过 2 倍，整张原图缩得更小时画出来会有摩尔纹）
 */
export function planLossless(p: LosslessInput): LosslessPlan {
  if (p.previewLossless || !needLossless(p.v, p.ft, p.dpr, p.W, p.H, p.previewLong)) return { kind: 'none' }
  const k = scaleK(p.v, p.ft, p.dpr)
  if (p.native && p.size !== null && p.size >= 0 && p.size <= FULL_MAX_BYTES && p.W * p.H <= FULL_MAX_PX && k >= 0.5) return { kind: 'full' }
  const level = levelFor(k, maxLevel(p.W, p.H))
  return { kind: 'tiles', level, tiles: visibleTiles(p.W, p.H, level, p.v, p.ft, p.cw, p.ch, p.ring ?? 1) }
}
