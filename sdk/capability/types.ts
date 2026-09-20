// 源头是 stream 仓库 `shared/capability/types.ts`；这里是拷贝，改契约先改那边再同步（规则见 ../README.md）。
//
// 能力包（desktop / netdisk / meituan…）与宿主之间的契约。**包不认识宿主**：它只吃一个
// `CapabilityContext`，谁把自己翻译成这七格谁就是宿主。翻译的实现只有一份，住 Stream 后端的
// `src/capabilities/host.ts`——内置能力（静态 import）与用户 `stream add` 装进来的可选包
// （从 `<dataDir>/recipes/` 动态 import）到场之后走同一个 `mount()`。
// 权威设计：docs/superpowers/specs/2026-09-06-stream-single-host-aggregation-design.md

export interface TextBlock {
  type: 'text'
  text: string
}

export interface ToolDef {
  name: string
  description: string
  /** DSH ParameterSchemaSpec: per-property JSON-Schema fragments + a `required: true` annotation
   *  (omit the key entirely for optional properties). */
  parameters: Record<string, Record<string, unknown>>
  output: { schema: { type: 'json' }; render(args: unknown, value: unknown): TextBlock[] }
  execute(args: Record<string, unknown>, exec?: { signal?: AbortSignal }): Promise<unknown>
  annotations?: { destructiveHint?: boolean; readOnlyHint?: boolean }
}

export interface CapabilityLog {
  info(msg: string): void
  warn(msg: string): void
}

export interface CapabilityContext {
  dataDir: string
  log: CapabilityLog
  require<T>(service: string): T | undefined
  provide(service: string, value: unknown): void
  registerTools(defs: ToolDef[]): void
  destructiveGate: 'host' | 'none'
  onDispose(fn: () => void | Promise<void>): void
}

/**
 * 一个能力的名字。**是任意字符串，不是一份固定名单**：可选能力包由用户自己 `stream add` 装进
 * `<dataDir>/recipes/`，宿主装载时才知道有谁——写死名单就等于「不在名单里的包静默不装」。
 * 撞名由宿主兜底：`registerTools` 与 `provide` 各自硬拒（`src/capabilities/host.ts`）。
 * 保留这个类型名而不是直接写 `string`，是因为它出现在契约上，读的人要能看出这一格的含义。
 */
export type CapabilityName = string

// 这里**没有 `credentials`**，是有意的：凭证域在 `package.json#stream.credentials` 申报，
// 不在模块上。理由是那一格过安装门（`credentialsSchema` 校验、确认页逐域点名让用户批准），
// 而模块级属性是安装之后才读得到的——它会让「用户批准的」和「实际取的」分成两份。
export interface Capability<C = Record<string, unknown>> {
  name: CapabilityName
  mount(ctx: CapabilityContext, config: C): Promise<void>
}
