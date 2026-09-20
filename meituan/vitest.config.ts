import { defineConfig } from 'vitest/config'

// 这份 suite 也被根 vitest.config.ts 收进 CI 的 `pnpm test`（同 capabilities/netdisk），所以不用
// `globals: true`：每个测试文件显式 `import { describe, it, expect } from 'vitest'`。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
