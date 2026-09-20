import { defineConfig } from 'tsdown'

/**
 * 一个入口、一个产物：`dist/index.js`——同 `@streamapp/netdisk`，理由见那份的头注
 * （文件名是能力槽位与安装门白名单共同认的那一个字面量）。
 *
 * 这个包本来就没有运行时依赖（美团官方 CLI 是运行期物化下来的脚本，不是 npm 依赖），
 * 所以自包含是天然的。
 */
export default defineConfig({
  entry: ['src/index.ts'],
  format: 'esm',
  outDir: 'dist',
  dts: false,
  noExternal: [/.*/],
  clean: true,
})
