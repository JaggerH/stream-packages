// meituan/src/index.test.ts
//
// 核心能力面（宿主中立）只做两件事：懒备 vendor、把四个动词交给 `ctx.registerTools`。
// 下单放不放行只看 `ctx.destructiveGate`——挂门是宿主那张脸的活（见 dsh.test.ts）。
import { describe, it, expect } from 'vitest'
import { EventEmitter } from 'node:events'
import { fakeCapabilityContext } from '../../sdk/capability/test-ctx.ts'
import { mount, capability, type ApplyDeps } from './index.ts'
import type { SpawnLike } from './cli.ts'
import { sha256Hex } from './vendor.ts'

/** 假 spawn：按子命令回 JSON。 */
function fakeSpawn(byCmd: Record<string, Record<string, unknown>>) {
  const cmds: string[] = []
  const spawnFn: SpawnLike = (_command, args) => {
    const cmd = args[1] ?? '?'
    cmds.push(cmd)
    const child = new EventEmitter() as EventEmitter & { stdout: EventEmitter; stderr: EventEmitter; kill: () => boolean }
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => true
    setTimeout(() => {
      child.stdout.emit('data', JSON.stringify(byCmd[cmd] ?? { ok: false, error: `unscripted ${cmd}` }))
      child.emit('close', 0)
    }, 0)
    return child as unknown as ReturnType<SpawnLike>
  }
  return { spawnFn, cmds }
}

const BYTES = new TextEncoder().encode('tarball')

function deps(o: { spawn?: SpawnLike } = {}): ApplyDeps {
  const present = new Set<string>()
  return {
    fetchFn: (async () => new Response(Buffer.from(BYTES))) as unknown as typeof fetch,
    ...(o.spawn ? { spawnFn: o.spawn } : {}),
    extract: async (_tgz, dest) => { present.add(`${dest}/scripts/run.js`) },
    vendorFs: { exists: (p) => present.has(p), mkdir: () => {}, writeFile: (p) => { present.add(p) }, rm: () => {} },
  }
}

const CONFIG = { dataDir: '/data/mt', vendor: { sha256: sha256Hex(BYTES) } }

const ORDER_ARGS = { productId: 'p', poiId: 's', cityId: '1', title: 't', poiName: 'n' }

const ORDER_SCRIPT = {
  init: { ok: true },
  'get-token': { ok: true, token: 'T' },
  'get-device-token': { ok: true, device_token: 'D' },
  order: { ok: true, orderId: 'O-1', payShortLink: 'https://pay' },
}

describe('mount', () => {
  it('把四个动词交给 registerTools', async () => {
    const ctx = fakeCapabilityContext()
    await mount(ctx, CONFIG, deps())
    expect(ctx.tools.map((t) => t.name).sort()).toEqual(['meituan_coupons', 'meituan_login', 'meituan_order', 'meituan_search'])
    // 判据是"已交给"不是"已挂上"：能力体调完 `ctx.registerTools` 就往下走了，它此刻还不知道
    // 宿主的 `tools` 服务在不在、defineTool 取不取得到——报成"已挂上"就是先报成功、后报跳过。
    // 真正的成功那一行由宿主（`src/capabilities/host.ts`）在注册循环之后打。
    expect(ctx.logs.info.join('\n')).toMatch(/已交给宿主注册/)
    expect(ctx.logs.info.join('\n')).not.toMatch(/已挂上/)
  })

  it('capability 是 name:meituan 的那一份，mount 同一条路', async () => {
    expect(capability.name).toBe('meituan')
    const ctx = fakeCapabilityContext()
    await capability.mount(ctx, {})
    expect(ctx.tools).toHaveLength(4)
  })

  it('第一次调动词才备 vendor（下载 + init），之后不再 init；动词真的跑到了 run.js', async () => {
    const f = fakeSpawn({ init: { ok: true }, 'get-token': { ok: true, token: 'T' } })
    const ctx = fakeCapabilityContext()
    await mount(ctx, CONFIG, deps({ spawn: f.spawnFn }))
    expect(f.cmds).toEqual([])
    const login = ctx.tools.find((t) => t.name === 'meituan_login')!
    expect(await login.execute({})).toEqual({ ok: true, loggedIn: true })
    expect(await login.execute({})).toEqual({ ok: true, loggedIn: true })
    expect(f.cmds).toEqual(['init', 'get-token', 'get-token'])
    expect(ctx.logs.info.join('\n')).toMatch(/美团 CLI 就绪/)
  })

  it('init 失败 → 动词回 error+hint，下一次调用会重试 init（不缓存失败）', async () => {
    let n = 0
    const spawnFn: SpawnLike = (...a) => {
      n++
      return fakeSpawn({ init: n === 1 ? { ok: false, error: 'PYTHON_NOT_FOUND' } : { ok: true }, 'get-token': { ok: true, token: 'T' } }).spawnFn(...a)
    }
    const ctx = fakeCapabilityContext()
    await mount(ctx, CONFIG, deps({ spawn: spawnFn }))
    const login = ctx.tools.find((t) => t.name === 'meituan_login')!
    expect(await login.execute({})).toMatchObject({ ok: false, error: expect.stringMatching(/PYTHON_NOT_FOUND/) })
    expect(await login.execute({})).toEqual({ ok: true, loggedIn: true })
  })

  it("destructiveGate:'host' → 下单一路跑到 run.js", async () => {
    const f = fakeSpawn(ORDER_SCRIPT)
    const ctx = fakeCapabilityContext({ destructiveGate: 'host' })
    await mount(ctx, CONFIG, deps({ spawn: f.spawnFn }))
    const r = (await ctx.tools.find((t) => t.name === 'meituan_order')!.execute(ORDER_ARGS)) as { ok: boolean; orderId?: string }
    expect(r).toMatchObject({ ok: true, orderId: 'O-1', payUrl: 'https://pay' })
    expect(f.cmds).toContain('order')
  })

  it("destructiveGate:'none' → 下单 fail closed，一条子命令都不发", async () => {
    const f = fakeSpawn(ORDER_SCRIPT)
    const ctx = fakeCapabilityContext({ destructiveGate: 'none' })
    await mount(ctx, CONFIG, deps({ spawn: f.spawnFn }))
    const r = (await ctx.tools.find((t) => t.name === 'meituan_order')!.execute(ORDER_ARGS)) as { ok: boolean; error: string }
    expect(r.ok).toBe(false)
    expect(r.error).toMatch(/审批门/)
    expect(f.cmds).toEqual([])
  })
})
