/**
 * 跑一次美团插件的 `scripts/run.js <子命令> …`，把它 stdout 上那一行 JSON 读回来。
 *
 * 美团那份 CLI 的契约（侦察自 run.js 源码，见 spec §1）：每个子命令**只在 stdout 打一行 JSON**
 * （`{ok, …}` 或 `{ok:false, error, …}`），退出码不可靠（`fail()` 也 exit 1，但成功路径的 `out()`
 * 一律 exit 0）。所以判据是「最后一行能 parse 成对象」，不是退出码。
 *
 * 子进程的 `HOME` / `USERPROFILE` 指到插件自己的目录（spec §4）：run.js 把 token 写到
 * `os.homedir()/.workbuddy/credentials/…`、cliguard 把自升级缓存写到 `~/.cliguard`——全部落在
 * 我们给的那个 home 里，不碰用户真正的 home。
 *
 * **绝不抛**：这里的每一种失败都落成 `{ok:false, error}`，由动词体决定怎么对模型说。
 */
import { spawn as nodeSpawn, type ChildProcess } from 'node:child_process'
import { join } from 'node:path'

/** 结构类型：真的 `child_process.spawn`，或测试替身。只声明我们用到的那几格。 */
export type SpawnLike = (
  command: string,
  args: string[],
  options: { cwd: string; env: Record<string, string | undefined>; stdio: ['ignore', 'pipe', 'pipe'] },
) => Pick<ChildProcess, 'stdout' | 'stderr' | 'on' | 'kill'>

export interface CliRunOptions {
  /** 解包后的 `scripts/` 目录（`run.js` 住这里）。 */
  scriptsDir: string
  /** 子进程当 HOME 用的目录（spec §4）。 */
  homeDir: string
  timeoutMs: number
  signal?: AbortSignal
  spawnFn?: SpawnLike
  /** 跑 run.js 用的 node；缺省 = 宿主自己这份 `process.execPath`。 */
  nodePath?: string
}

export type CliResult =
  | { ok: true; json: Record<string, unknown>; stdout: string; stderr: string }
  | { ok: false; error: string; stdout: string; stderr: string; timedOut?: boolean }

/** 从 stdout 里取**最后一行**能 parse 成对象的 JSON（run.js 的 `out()` 只打一行；cliguard 偶尔往 stdout 打提示）。 */
export function parseCliStdout(stdout: string): Record<string, unknown> | undefined {
  const lines = stdout.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]!
    if (!line.startsWith('{')) continue
    try {
      const v = JSON.parse(line) as unknown
      if (v && typeof v === 'object' && !Array.isArray(v)) return v as Record<string, unknown>
    } catch {
      /* 不是这一行 */
    }
  }
  return undefined
}

/** `HOME`/`USERPROFILE` 全换成插件目录；`NODE_OPTIONS` 清空（run.js 自己也这么做——宿主的 loader 钩子别灌进去）。 */
export function cliEnv(base: Record<string, string | undefined>, homeDir: string): Record<string, string | undefined> {
  return { ...base, HOME: homeDir, USERPROFILE: homeDir, NODE_OPTIONS: '', WORKBUDDY_CLIENT_TYPE: undefined }
}

export async function runCli(command: string, args: string[], opts: CliRunOptions): Promise<CliResult> {
  const spawnFn = opts.spawnFn ?? (nodeSpawn as unknown as SpawnLike)
  const runJs = join(opts.scriptsDir, 'run.js')
  let stdout = ''
  let stderr = ''
  let child: ReturnType<SpawnLike>
  try {
    child = spawnFn(opts.nodePath ?? process.execPath, [runJs, command, ...args], {
      cwd: opts.scriptsDir,
      env: cliEnv(process.env, opts.homeDir),
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  } catch (err) {
    return { ok: false, error: `起不来 run.js：${err instanceof Error ? err.message : String(err)}`, stdout, stderr }
  }
  child.stdout?.on('data', (c: Buffer | string) => { stdout += String(c) })
  child.stderr?.on('data', (c: Buffer | string) => { stderr += String(c) })

  const outcome = await new Promise<{ kind: 'exit'; code: number | null } | { kind: 'error'; err: Error } | { kind: 'timeout' } | { kind: 'aborted' }>((resolve) => {
    let done = false
    const finish = (o: { kind: 'exit'; code: number | null } | { kind: 'error'; err: Error } | { kind: 'timeout' } | { kind: 'aborted' }) => {
      if (done) return
      done = true
      clearTimeout(timer)
      opts.signal?.removeEventListener('abort', onAbort)
      resolve(o)
    }
    const onAbort = () => { try { child.kill('SIGKILL') } catch { /* 已退出 */ } finish({ kind: 'aborted' }) }
    const timer = setTimeout(() => { try { child.kill('SIGKILL') } catch { /* 已退出 */ } finish({ kind: 'timeout' }) }, opts.timeoutMs)
    if (opts.signal?.aborted) onAbort()
    else opts.signal?.addEventListener('abort', onAbort, { once: true })
    child.on('error', (err: Error) => finish({ kind: 'error', err }))
    child.on('close', (code: number | null) => finish({ kind: 'exit', code }))
  })

  if (outcome.kind === 'timeout') return { ok: false, error: `run.js ${command} 超过 ${opts.timeoutMs}ms 没回来，已终止`, stdout, stderr, timedOut: true }
  if (outcome.kind === 'aborted') return { ok: false, error: `run.js ${command} 被取消`, stdout, stderr }
  if (outcome.kind === 'error') return { ok: false, error: `run.js ${command} 起不来：${outcome.err.message}`, stdout, stderr }
  const json = parseCliStdout(stdout)
  if (!json) {
    const tail = (stderr || stdout).trim().split(/\r?\n/).slice(-3).join(' | ')
    return { ok: false, error: `run.js ${command} 没有输出 JSON（exit ${outcome.code ?? '?'}）${tail ? `：${tail}` : ''}`, stdout, stderr }
  }
  return { ok: true, json, stdout, stderr }
}
