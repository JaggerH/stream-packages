#!/usr/bin/env node
/**
 * publish 闸（带代码的包挂在 `prepack`）：断言 `dist/index.js` 在盘上且非空，并且
 * `npm pack --dry-run` 出的 tarball **恰好**是 `package.json` / `README.md` / `dist/index.js` 三个文件。
 *
 * 为什么：产物在 `.gitignore` 里，干净检出上 `npm publish` 会成功发出一个空壳包——`npm install`
 * 照样成功，一直到 Stream 后端动态 import `dist/index.js` 才报「没装上」。另一头，Stream 的安装门
 * 只放行这三个路径（代码槽位只认 `dist/index.js` 一个字面量）：tsdown 切出的第二个 chunk、
 * 上一代残留、sourcemap，进了 tarball 都会让用户那边的安装被整包拒掉。两种都是「本地全绿、
 * 用户那边坏掉」，所以判据落在 publish 那一刻。
 *
 * 用法：`node scripts/assert-npm-artifact.mjs [<包目录>]`——不传就是 cwd（`prepack` 那一刻 cwd 正是被打包的包）。
 */
import { readFileSync, statSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'

const pkgDir = resolve(process.argv[2] ?? process.cwd())
const pkg = JSON.parse(readFileSync(join(pkgDir, 'package.json'), 'utf8'))
const ALLOWED = ['README.md', 'dist/index.js', 'package.json']
const problems = []

const entry = join(pkgDir, 'dist', 'index.js')
if (!existsSync(entry) || statSync(entry).size === 0) {
  problems.push('dist/index.js 不存在或是空文件——先 `npm run bundle`')
}

// `--ignore-scripts`：这个脚本自己就挂在 prepack 上，不带它 dry-run 会再触发 prepack、无限递归。
const pack = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: pkgDir, encoding: 'utf8' })
if (pack.status !== 0) {
  problems.push(`npm pack --dry-run 失败：${(pack.stderr || pack.stdout).trim()}`)
} else {
  const listed = JSON.parse(pack.stdout)[0].files.map((f) => f.path).sort()
  const expected = [...ALLOWED].sort()
  if (JSON.stringify(listed) !== JSON.stringify(expected)) {
    problems.push(`tarball 清单是 [${listed.join(', ')}]，必须恰好是 [${expected.join(', ')}]——多出来的会让 Stream 安装门拒掉整个包，少了的装上也起不来`)
  }
}

if (problems.length > 0) {
  console.error(`[assert-npm-artifact] ${pkg.name} 还不能发，拒绝打包：\n` + problems.map((p) => `  - ${p}`).join('\n'))
  process.exit(1)
}
console.log(`[assert-npm-artifact] ${pkg.name}: dist/index.js 非空，tarball 恰好 ${ALLOWED.join(' / ')}，放行。`)
