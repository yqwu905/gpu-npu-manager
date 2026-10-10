// 示例数据模式下的图片：按 /image 接口的约定程序化生成缩略图、预览、原图与瓦片，不发请求。
// 序号逢 10 的图为 7680×4320，其余 3840×2160；像素 px(x, y) = [(x·7 + seed) & 255, (y·5) & 255, ((x ^ y)·3) & 255]，
// seed 为 "id|path" 的哈希。第 0 层瓦片逐像素精确，第 l 层取 (2^l·x, 2^l·y)，缩略图与预览按尺寸点采样
import type { ImageKind } from '../api/types'
import { TILE, bucket, levelDims, maxLevel, previewDims } from '../pages/compare/geometry.ts'

/** 与响应头 X-Image-* 对应 */
export interface MockMeta { W: number; H: number; format: string; native: boolean; lossless: boolean; normalized: boolean; tile: number }
export interface MockImage { w: number; h: number; data: Uint8ClampedArray; meta: MockMeta }
export interface MockParams { size?: number; l?: number; x?: number; y?: number }

/** 模拟的生成耗时（毫秒），让加载状态能被看到 */
const DELAY: Record<ImageKind, number> = { thumb: 20, preview: 60, tile: 30, full: 120 }

function hash(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619)
  return h >>> 0
}

/** 文件名里最后一段数字作为序号，没有时用路径哈希 */
function serial(path: string): number {
  const m = /(\d+)\D*$/.exec(path)
  return m ? Number(m[1]) : hash(path)
}

export const mockDims = (path: string): [number, number] => (serial(path) % 10 === 0 ? [7680, 4320] : [3840, 2160])
export const mockSeed = (id: number, path: string) => hash(`${id}|${path}`) & 255
export const mockPixel = (seed: number, x: number, y: number): [number, number, number] => [(x * 7 + seed) & 255, (y * 5) & 255, ((x ^ y) * 3) & 255]

/** 输出 (i, j) 取原图 (sx(i), sy(j)) 的像素 */
function sample(seed: number, w: number, h: number, sx: (i: number) => number, sy: (j: number) => number): Uint8ClampedArray {
  const data = new Uint8ClampedArray(w * h * 4)
  const xs = new Int32Array(w)
  for (let i = 0; i < w; i++) xs[i] = sx(i)
  for (let j = 0, o = 0; j < h; j++) {
    const y = sy(j), g = (y * 5) & 255
    for (let i = 0; i < w; i++, o += 4) {
      const x = xs[i]
      data[o] = (x * 7 + seed) & 255
      data[o + 1] = g
      data[o + 2] = ((x ^ y) * 3) & 255
      data[o + 3] = 255
    }
  }
  return data
}

/** 与真实接口相同的失败形式：带 HTTP 状态码 */
export class MockHttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export const mockSource = {
  dims: mockDims,
  delay: (kind: ImageKind) => DELAY[kind],
  /** 原图 (x, y) 处的像素，e2e 校验放大后的像素时用 */
  pixel: (id: number, path: string, x: number, y: number) => mockPixel(mockSeed(id, path), x, y),
  /** 同步生成一张图；参数与 /image 一致，瓦片越界抛出 400 */
  render(id: number, path: string, kind: ImageKind, p: MockParams = {}): MockImage {
    const [W, H] = mockDims(path), seed = mockSeed(id, path)
    const meta: MockMeta = { W, H, format: 'png', native: true, lossless: kind !== 'thumb', normalized: false, tile: 0 }
    if (kind === 'tile') {
      const l = p.l ?? 0, x = p.x ?? 0, y = p.y ?? 0
      const [Wl, Hl] = levelDims(W, H, l)
      if (!(l >= 0 && l <= maxLevel(W, H) && x >= 0 && y >= 0 && x * TILE < Wl && y * TILE < Hl)) throw new MockHttpError(400, '瓦片超出范围')
      const x0 = x * TILE, y0 = y * TILE, s = 2 ** l
      const w = Math.min(TILE, Wl - x0), h = Math.min(TILE, Hl - y0)
      return { w, h, data: sample(seed, w, h, (i) => (x0 + i) * s, (j) => (y0 + j) * s), meta: { ...meta, tile: TILE } }
    }
    const s = kind === 'thumb' ? 256 : kind === 'preview' ? bucket(p.size ?? 2048) : Math.max(W, H)
    const [w, h] = previewDims(W, H, s)
    if (kind === 'preview') meta.lossless = Math.max(W, H) <= s
    return { w, h, data: sample(seed, w, h, (i) => Math.floor((i * W) / w), (j) => Math.floor((j * H) / h)), meta }
  },
}
