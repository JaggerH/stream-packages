// 源头是 stream 仓库 `shared/capability/test-ctx.ts`；这里是拷贝，改契约先改那边再同步（规则见 ../README.md）。
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { CapabilityContext, ToolDef } from './types.js'

export interface FakeCapabilityContext extends CapabilityContext {
  tools: ToolDef[]
  services: Map<string, unknown>
  disposers: Array<() => void | Promise<void>>
  logs: { info: string[]; warn: string[] }
  dispose(): Promise<void>
}

/** 内存版 `CapabilityContext`，供 Task 3/4/6/7 四个能力包的测试共用——不经 MCP/DSH 任何一份翻译，
 *  直接断言包 `mount(ctx, config)` 调用了哪些 `registerTools`/`provide`/`require`/`onDispose`。 */
export function fakeCapabilityContext(opts: { dataDir?: string; destructiveGate?: 'host' | 'none' } = {}): FakeCapabilityContext {
  const dataDir = opts.dataDir ?? mkdtempSync(join(tmpdir(), 'stream-capability-test-'))
  const tools: ToolDef[] = []
  const services = new Map<string, unknown>()
  const disposers: Array<() => void | Promise<void>> = []
  const logs: { info: string[]; warn: string[] } = { info: [], warn: [] }

  return {
    dataDir,
    log: {
      info(msg: string) {
        logs.info.push(msg)
      },
      warn(msg: string) {
        logs.warn.push(msg)
      },
    },
    require<T>(service: string): T | undefined {
      return services.get(service) as T | undefined
    },
    provide(service: string, value: unknown) {
      services.set(service, value)
    },
    registerTools(defs: ToolDef[]) {
      tools.push(...defs)
    },
    destructiveGate: opts.destructiveGate ?? 'none',
    onDispose(fn: () => void | Promise<void>) {
      disposers.push(fn)
    },
    tools,
    services,
    disposers,
    logs,
    async dispose() {
      // 逆序、逐个吞异常——和宿主（`src/capabilities/host.ts`）的真实清理顺序一致。
      for (let i = disposers.length - 1; i >= 0; i--) {
        try {
          await disposers[i]!()
        } catch {
          // swallow
        }
      }
    },
  }
}
