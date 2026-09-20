// meituan/src/cli.test.ts
//
// run.js 的契约是「stdout 最后一行 JSON」，退出码不可信；子进程的 HOME 必须换成插件目录。
import { describe, it, expect } from 'vitest'
import { EventEmitter } from 'node:events'
import { cliEnv, parseCliStdout, runCli, type SpawnLike } from './cli.ts'

/** 假子进程：按脚本吐 stdout/stderr 然后 close。 */
function fakeSpawn(script: { stdout?: string; stderr?: string; code?: number | null; delayMs?: number; hang?: boolean }) {
  const calls: Array<{ command: string; args: string[]; cwd: string; env: Record<string, string | undefined> }> = []
  let killed = 0
  const spawnFn: SpawnLike = (command, args, options) => {
    calls.push({ command, args, cwd: options.cwd, env: options.env })
    const child = new EventEmitter() as EventEmitter & { stdout: EventEmitter; stderr: EventEmitter; kill: () => boolean }
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => { killed++; return true }
    setTimeout(() => {
      if (script.hang) return
      if (script.stdout) child.stdout.emit('data', script.stdout)
      if (script.stderr) child.stderr.emit('data', script.stderr)
      child.emit('close', script.code ?? 0)
    }, script.delayMs ?? 0)
    return child as unknown as ReturnType<SpawnLike>
  }
  return { spawnFn, calls, killed: () => killed }
}

const OPTS = { scriptsDir: '/data/vendor/abc/scripts', homeDir: '/data/home', timeoutMs: 500 }

describe('parseCliStdout', () => {
  it('取最后一行 JSON 对象；前面 cliguard 的提示行不算', () => {
    expect(parseCliStdout('[cliguard] patched\n{"ok":true,"token":"t"}\n')).toEqual({ ok: true, token: 't' })
  })
  it('没有 JSON 行 → undefined（不猜）', () => {
    expect(parseCliStdout('Token: abc\n')).toBeUndefined()
    expect(parseCliStdout('')).toBeUndefined()
  })
})

describe('cliEnv', () => {
  it('HOME / USERPROFILE 换成插件目录，NODE_OPTIONS 清空，WORKBUDDY_CLIENT_TYPE 不透传', () => {
    const env = cliEnv({ PATH: '/bin', HOME: '/home/me', NODE_OPTIONS: '--import x', WORKBUDDY_CLIENT_TYPE: 'mac' }, '/data/home')
    expect(env.HOME).toBe('/data/home')
    expect(env.USERPROFILE).toBe('/data/home')
    expect(env.NODE_OPTIONS).toBe('')
    expect(env.WORKBUDDY_CLIENT_TYPE).toBeUndefined()
    expect(env.PATH).toBe('/bin')
  })
})

describe('runCli', () => {
  it('拼 `node run.js <cmd> …`，cwd 是 scripts 目录，读回那一行 JSON', async () => {
    const f = fakeSpawn({ stdout: '{"ok":true,"type":"auth_link","url":"https://x"}\n' })
    const r = await runCli('auth-get-code', [], { ...OPTS, spawnFn: f.spawnFn, nodePath: '/usr/bin/node' })
    expect(r).toMatchObject({ ok: true, json: { type: 'auth_link', url: 'https://x' } })
    expect(f.calls[0]).toMatchObject({ command: '/usr/bin/node', args: ['/data/vendor/abc/scripts/run.js', 'auth-get-code'], cwd: '/data/vendor/abc/scripts' })
    expect(f.calls[0]!.env.HOME).toBe('/data/home')
  })

  it('exit 1 但 stdout 有 JSON → 照样 ok（run.js 的 fail() 也打 JSON）', async () => {
    const f = fakeSpawn({ stdout: '{"ok":false,"error":"NO_TOKEN"}\n', code: 1 })
    const r = await runCli('issue', [], { ...OPTS, spawnFn: f.spawnFn })
    expect(r).toMatchObject({ ok: true, json: { ok: false, error: 'NO_TOKEN' } })
  })

  it('没有 JSON → ok:false 带 stderr 尾巴', async () => {
    const f = fakeSpawn({ stdout: '', stderr: 'Error: Cannot find module x\n', code: 1 })
    const r = await runCli('issue', [], { ...OPTS, spawnFn: f.spawnFn })
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toMatch(/Cannot find module/)
  })

  it('超时 → kill + timedOut', async () => {
    const f = fakeSpawn({ hang: true })
    const r = await runCli('auth-poll-token', [], { ...OPTS, timeoutMs: 30, spawnFn: f.spawnFn })
    expect(r).toMatchObject({ ok: false, timedOut: true })
    expect(f.killed()).toBe(1)
  })

  it('exec.signal 触发 → kill + 被取消', async () => {
    const f = fakeSpawn({ hang: true })
    const ac = new AbortController()
    const p = runCli('search', [], { ...OPTS, timeoutMs: 5000, spawnFn: f.spawnFn, signal: ac.signal })
    ac.abort()
    const r = await p
    expect(r.ok).toBe(false)
    if (!r.ok) expect(r.error).toMatch(/取消/)
    expect(f.killed()).toBe(1)
  })
})
