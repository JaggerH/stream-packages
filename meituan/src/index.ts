/**
 * `@streamapp/meituan` 的**宿主中立能力面**——把美团官方给 WorkBuddy 的「美团生活助手」CLI
 * 包成四个动词：登录 / 领券 / 到店团购搜索 / 下单
 * （spec 在 stream 仓库 `docs/superpowers/specs/2026-09-04-meituan-dsh-plugin-design.md`）。
 *
 * 这一层只认 `CapabilityContext`（`sdk/capability/types.ts`，stream 仓库 `shared/capability/types.ts`
 * 的拷贝），**不认识任何宿主**：没有 cordis 的
 * `effect` / `inject` / `on`，没有 `@deepseek-ai/dsh-tools`。DSH 那张脸在 `./dsh.ts`。
 *
 * 美团的字节不在这个包里：第一次调任何动词时 `ensureVendor` 从美团公开桶下载、校验钉死的 sha256、
 * 解包、跑它自己的 `run.js init`（`vendor.ts`）。之后每个动词 = spawn 一次 `node run.js <cmd>`（`cli.ts`）。
 *
 * 下单的人工确认**只能来自宿主**（spec 2026-09-05 §4.1）：核心把 `meituan_order` 标成
 * `destructiveHint`，并按 `ctx.destructiveGate` 判自己该不该放行——`'host'` 表示宿主确实会拦一道
 * （DSH 脸挂上了 `tools/pre-execute`，或 MCP 宿主本来就弹权限确认），`'none'` 就 fail closed：
 * 没有人来问用户，就不花钱。
 */
import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'
import type { Capability, CapabilityContext } from '../../sdk/capability/types.ts'
import { runCli, type CliResult, type SpawnLike } from './cli.ts'
import { ensureVendor, type VendorConfig, type VendorDeps, type VendorResult } from './vendor.ts'
import { meituanToolOptions, TOOL_NAMES, type MeituanSurfaceDeps } from './tools.ts'

export { TOOL_NAMES, orderGate } from './tools.ts'
export { VENDOR_SHA256, VENDOR_URL } from './vendor.ts'

/** 这一行在 profile 里收的 config。 */
export interface MeituanHostConfig {
  /** 插件自己的 data 目录（美团 CLI 解包 + 它的 token / 缓存都落这里）；缺省 `~/.stream-meituan-plugin`。 */
  dataDir?: string
  /** 覆盖美团包的下载地址 / 钉死的 sha256（升级时人核过之后改；缺省用 `vendor.ts` 里那份）。 */
  vendor?: VendorConfig
}

function defaultDataDir(): string {
  return join(homedir(), '.stream-meituan-plugin')
}

/** 注入点，只为测试。 */
export interface ApplyDeps {
  fetchFn: typeof fetch
  spawnFn?: SpawnLike
  /** 解 .tar.gz；缺省 = 系统 `tar`。 */
  extract?: VendorDeps['extract']
  /** vendor 层的文件系统注入（只为测试）。 */
  vendorFs?: Pick<VendorDeps, 'exists' | 'mkdir' | 'writeFile' | 'rm'>
}

/** 系统 `tar`（Linux / macOS 自带；Windows 10+ 自带 bsdtar）。 */
export function extractWithSystemTar(tgzPath: string, destDir: string, spawnFn: SpawnLike = spawn as unknown as SpawnLike): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let stderr = ''
    let child: ReturnType<SpawnLike>
    try {
      child = spawnFn('tar', ['-xzf', tgzPath, '-C', destDir], { cwd: destDir, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (err) {
      reject(err instanceof Error ? err : new Error(String(err)))
      return
    }
    child.stderr?.on('data', (c: Buffer | string) => { stderr += String(c) })
    child.on('error', (err: Error) => reject(err))
    child.on('close', (code: number | null) => (code === 0 ? resolve() : reject(new Error(`tar 退出 ${code ?? '?'}${stderr ? `：${stderr.trim().split('\n').slice(-2).join(' | ')}` : ''}`))))
  })
}

export const defaultDeps: ApplyDeps = {
  fetchFn: (input, init) => fetch(input, init),
}

/**
 * 能力体。顺序：备 vendor（懒，第一次用才下载）→ 交出四个动词。
 * 任何一步失败都只记日志、不抛（跑在宿主自己的进程里，逃出去的异常带走整个宿主）。
 */
export async function mount(
  ctx: CapabilityContext,
  config: MeituanHostConfig = {},
  deps: ApplyDeps = defaultDeps,
): Promise<void> {
  try {
    const dataDir = config.dataDir?.trim() || ctx.dataDir || defaultDataDir()
    const spawnFn = deps.spawnFn
    const extract = deps.extract ?? ((tgz, dest) => extractWithSystemTar(tgz, dest))

    // vendor：**懒**且串行——第一次真用到时才下载；并发调用共享同一个 promise，失败后下次重试。
    let inflight: Promise<VendorResult> | undefined
    const ensure = (): Promise<VendorResult> => {
      if (inflight) return inflight
      const vendorDeps: VendorDeps = {
        fetchFn: deps.fetchFn,
        extract,
        runInit: async (scriptsDir, homeDir) => {
          const r = await runCli('init', [], spawnFn ? { scriptsDir, homeDir, timeoutMs: 120_000, spawnFn } : { scriptsDir, homeDir, timeoutMs: 120_000 })
          if (!r.ok) return { ok: false, error: r.error }
          if (r.json.ok !== true) return { ok: false, error: String(r.json.error ?? JSON.stringify(r.json)) }
          return { ok: true }
        },
        ...(deps.vendorFs ?? {}),
      }
      inflight = ensureVendor(dataDir, config.vendor ?? {}, vendorDeps).then((r) => {
        if (!r.ok) inflight = undefined
        else ctx.log.info(`美团 CLI 就绪：${r.scriptsDir}（sha256 ${r.sha256.slice(0, 12)}）`)
        return r
      })
      return inflight
    }

    const surface: MeituanSurfaceDeps = {
      ensure,
      run: async (cmd, args, opts): Promise<CliResult> => {
        const v = await ensure()
        if (!v.ok) return { ok: false, error: v.error, stdout: '', stderr: '' }
        return runCli(cmd, args, { scriptsDir: v.scriptsDir, homeDir: v.homeDir, timeoutMs: opts.timeoutMs, ...(opts.signal ? { signal: opts.signal } : {}), ...(spawnFn ? { spawnFn } : {}) })
      },
      // 宿主说得有人拦这一道（`'host'`）才下单；`'none'` = 没有人来问用户 → fail closed。
      hasOrderGate: () => ctx.destructiveGate === 'host',
    }

    ctx.registerTools(meituanToolOptions(surface))
    // 说"已交给"不说"已挂上"：真挂没挂上归 `ctx.registerTools`，它在 DSH 那张脸里还要等
    // `tools` 服务到位、还要 import 到 defineTool，两处都可能跳过并各自 warn，而且是在这一句
    // **之后**。报成"已挂上"就是替宿主打一份它没做过的包票，日志顺序还会是"先成功、后跳过"。
    ctx.log.info(`${TOOL_NAMES.length} 个美团动词已交给宿主注册：${TOOL_NAMES.join(' / ')}。`)
  } catch (err) {
    ctx.log.warn(`装载失败，已跳过（不阻塞宿主装载）：${err instanceof Error ? err.message : String(err)}`)
  }
}

export const capability: Capability<MeituanHostConfig> = {
  name: 'meituan',
  mount: (ctx, config) => mount(ctx, config),
}
