# sdk/ — 能力包吃的那份宿主契约（拷贝）

带代码的包（今天只有 `meituan/`）只认一个类型契约：`CapabilityContext` / `ToolDef`（`sdk/capability/types.ts`），
测试用内存版 `fakeCapabilityContext`（`sdk/capability/test-ctx.ts`）。它们**不发 npm**，包 import 的是
本仓库里的相对路径（`../../sdk/capability/types.ts`），tsdown 打包时 `types.ts` 只剩类型、进不了产物。

## 同步规则

- **源头是 stream 仓库 `shared/capability/`**，这里的两个文件是逐字拷贝（只多一行头注）。
- 改契约**先改 stream 那边**，再把文件整份拷过来覆盖。别在这里单独改：宿主（stream 后端 `src/capabilities/host.ts`）
  按它那份翻译 ctx，这里改了宿主不会跟着变，包在 typecheck 里绿、装进 Stream 后调不到那一格。
- stream 侧 `shared/capability/types.ts` 头注写着「stream-packages/sdk/capability/ 持有一份拷贝」，改它的人从那儿被指回来。
- 核对是否漂了：`diff <stream>/shared/capability/types.ts sdk/capability/types.ts` 应只差头注那两行。
