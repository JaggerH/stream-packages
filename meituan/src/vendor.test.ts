// meituan/src/vendor.test.ts
//
// 钉哈希的绊线是这个文件存在的理由：对 → 解包 + init；错 → 一个字节都不解；已在场 → 零网络。
import { describe, it, expect } from 'vitest'
import { join } from 'node:path'
import { ensureVendor, sha256Hex, vendorPaths, type VendorDeps } from './vendor.ts'

const DATA = '/data/mt'
const BYTES = new TextEncoder().encode('fake-tarball')
const GOOD = sha256Hex(BYTES)
const P = vendorPaths(DATA, GOOD)
const CFG = { sha256: GOOD }

function harness(o: { present?: string[]; status?: number; initOk?: boolean; extractFails?: boolean; bytes?: Uint8Array } = {}) {
  const present = new Set(o.present ?? [])
  const fetched: string[] = []
  const extracted: string[] = []
  const inits: string[] = []
  const written: string[] = []
  const chmods: Array<[string, number]> = []
  const deps: VendorDeps = {
    fetchFn: (async (u: string | URL) => {
      fetched.push(String(u))
      return new Response(Buffer.from(o.bytes ?? BYTES), { status: o.status ?? 200 })
    }) as unknown as typeof fetch,
    async extract(tgz, dest) {
      if (o.extractFails) throw new Error('tar: not found')
      extracted.push(`${tgz}→${dest}`)
      present.add(join(dest, 'scripts', 'run.js'))
    },
    async runInit(scriptsDir) {
      inits.push(scriptsDir)
      return o.initOk === false ? { ok: false, error: 'PYTHON_NOT_FOUND' } : { ok: true }
    },
    exists: (p) => present.has(p),
    mkdir: () => {},
    chmod: (p, mode) => { chmods.push([p, mode]) },
    writeFile: (p) => { written.push(p); present.add(p) },
    rm: (p) => { for (const x of [...present]) if (x.startsWith(p)) present.delete(x) },
  }
  return { deps, fetched, extracted, inits, written, present, chmods }
}

describe('ensureVendor', () => {
  it('每次都把 home 目录收成 0700（登录 token 就在它底下），已就绪那条路也收', async () => {
    const h = harness({ present: [P.marker, join(P.scriptsDir, 'run.js')] })
    await ensureVendor(DATA, CFG, h.deps)
    expect(h.chmods).toEqual([[P.homeDir, 0o700]])
    expect(h.fetched).toHaveLength(0)
  })

  it('每次都在 home 目录写一堵 CommonJS 边界墙（cliguard 自升级出来的 CJS 会被宿主的 type:module 毒死）', async () => {
    const h = harness({ present: [P.marker, join(P.scriptsDir, 'run.js')] })
    await ensureVendor(DATA, CFG, h.deps)
    expect(h.written).toContain(join(P.homeDir, 'package.json'))
  })

  it('平台不支持 chmod → 照常成功（不是错）', async () => {
    const h = harness({ present: [P.marker, join(P.scriptsDir, 'run.js')] })
    const r = await ensureVendor(DATA, CFG, { ...h.deps, chmod: () => { throw new Error('EPERM') } })
    expect(r.ok).toBe(true)
  })

  it('哈希对 → 下载、解包、init、写标记，回 scripts/home 两个路径', async () => {
    const h = harness()
    const r = await ensureVendor(DATA, CFG, { ...h.deps, fetchFn: h.deps.fetchFn })
    expect(r).toEqual({ ok: true, scriptsDir: P.scriptsDir, homeDir: P.homeDir, sha256: GOOD })
    expect(h.extracted).toHaveLength(1)
    expect(h.inits).toEqual([P.scriptsDir])
    expect(h.written).toContain(P.marker)
  }, 10_000)

  it('哈希不对 → 一个字节都不解、init 不跑，错误里写明期望/实得，hint 指向 pin', async () => {
    const h = harness({ bytes: new TextEncoder().encode('tampered') })
    const r = await ensureVendor(DATA, CFG, h.deps)
    expect(r.ok).toBe(false)
    if (!r.ok) {
      expect(r.error).toMatch(/上游包变了/)
      expect(r.error).toContain(GOOD.slice(0, 12))
      expect(r.hint).toMatch(/VENDOR_SHA256/)
    }
    expect(h.extracted).toHaveLength(0)
    expect(h.inits).toHaveLength(0)
  })

  it('已在场且 init 过 → 零网络直接回', async () => {
    const h = harness({ present: [P.marker, join(P.scriptsDir, 'run.js')] })
    const r = await ensureVendor(DATA, CFG, h.deps)
    expect(r.ok).toBe(true)
    expect(h.fetched).toHaveLength(0)
    expect(h.inits).toHaveLength(0)
  })

  it('解包在场但 init 没成过 → 不重下，只重跑 init', async () => {
    const h = harness({ present: [join(P.scriptsDir, 'run.js')] })
    const r = await ensureVendor(DATA, CFG, h.deps)
    expect(r.ok).toBe(true)
    expect(h.fetched).toHaveLength(0)
    expect(h.inits).toHaveLength(1)
  })

  it('init 失败 → ok:false，hint 说清要 Node/npm/Python，标记不写（下次再试）', async () => {
    const h = harness({ initOk: false })
    const r = await ensureVendor(DATA, CFG, h.deps)
    expect(r.ok).toBe(false)
    if (!r.ok) {
      expect(r.error).toContain('PYTHON_NOT_FOUND')
      expect(r.hint).toMatch(/Python 3/)
    }
    expect(h.written).not.toContain(P.marker)
  })

  it('下载 HTTP 非 200 / 解包失败 → 各自说人话，解包失败要清掉半截目录', async () => {
    const a = await ensureVendor(DATA, CFG, harness({ status: 404 }).deps)
    expect(a.ok).toBe(false)
    if (!a.ok) expect(a.error).toMatch(/HTTP 404/)
    const h = harness({ extractFails: true })
    const b = await ensureVendor(DATA, CFG, h.deps)
    expect(b.ok).toBe(false)
    if (!b.ok) expect(b.hint).toMatch(/tar/)
    expect([...h.present].some((p) => p.startsWith(P.root))).toBe(false)
  })

  it('config 里给了 url/sha256 → 覆盖默认 pin（升级时人核过之后改这里）', async () => {
    const other = new TextEncoder().encode('v2')
    const h = harness({ bytes: other })
    const r = await ensureVendor(DATA, { url: 'https://example/v2.tgz', sha256: sha256Hex(other) }, h.deps)
    expect(r.ok).toBe(true)
    expect(h.fetched).toEqual(['https://example/v2.tgz'])
  })
})
