/**
 * 美团的字节怎么来（spec §4）：**不进我们的包**，第一次用时从美团自己的公开桶下载、钉死 sha256、
 * 解到插件的 data 目录，再跑一次它自己的 `run.js init`（装 pt-passport 登录 CLI）。
 *
 * 钉哈希是故意的绊线：那是一段混淆代码（pt-passport + cliguard，还带自升级守护进程），
 * 它的更新权不该交给上游。哈希不对 → 不解包、不执行，回一句「上游包变了，需要人核一遍再更新 pin」。
 *
 * 布局：
 *   <dataDir>/vendor/<sha256 前 12 位>/        解包根（scripts/ agents/ references/ …）
 *   <dataDir>/vendor/<sha256 前 12 位>/.init-ok  `run.js init` 成功过的标记
 *   <dataDir>/home/                             子进程的 HOME（token / cliguard 缓存落这里）
 *
 * 每一格都**绝不抛**：失败落成 `{ok:false, error, hint}`。
 */
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync, rmSync, chmodSync } from 'node:fs'
import { join } from 'node:path'

/** 2026-09-04 从美团公开桶下载并逐文件读过的那一份（spec §1）。换 pin = 有人重新核过新包。 */
export const VENDOR_URL = 'https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace/bundles/meituan-living-assistant.tar.gz'
export const VENDOR_SHA256 = 'ace91900ea01066b1557880e1563488c0537824632ec351c77b5f3a1b31a1205'

export interface VendorDeps {
  fetchFn: typeof fetch
  /** 把一个 .tar.gz 解到目录里（缺省 = 系统 `tar`，见 index.ts）。 */
  extract(tgzPath: string, destDir: string): Promise<void>
  /** 跑 `run.js init`（装 pt-passport）；返回它那一行 JSON 或失败。 */
  runInit(scriptsDir: string, homeDir: string): Promise<{ ok: boolean; error?: string }>
  exists?: (p: string) => boolean
  mkdir?: (p: string) => void
  /** 收紧目录权限（登录 token 就落在 homeDir 下）；平台不支持时静默跳过。 */
  chmod?: (p: string, mode: number) => void
  writeFile?: (p: string, data: Uint8Array | string) => void
  rm?: (p: string) => void
}

export interface VendorConfig {
  url?: string
  sha256?: string
}

export type VendorResult =
  | { ok: true; scriptsDir: string; homeDir: string; sha256: string }
  | { ok: false; error: string; hint: string }

export function vendorPaths(dataDir: string, sha256: string): { root: string; scriptsDir: string; homeDir: string; marker: string } {
  const root = join(dataDir, 'vendor', sha256.slice(0, 12))
  return { root, scriptsDir: join(root, 'scripts'), homeDir: join(dataDir, 'home'), marker: join(root, '.init-ok') }
}

export function sha256Hex(bytes: Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex')
}

const PIN_HINT = '这是故意的绊线：那份包里有混淆过的登录 CLI 和自升级守护进程，换了就得有人重新读一遍再更新 VENDOR_SHA256（stream-packages/meituan/src/vendor.ts）。'

/**
 * 备好美团的 CLI。已在场且 init 过 → 直接回路径（零网络）；否则下载 → 校验 → 解包 → init。
 * 并发调用由调用方串行化（index.ts 里一个 promise 缓存），这里不管。
 */
export async function ensureVendor(dataDir: string, config: VendorConfig, deps: VendorDeps): Promise<VendorResult> {
  const url = config.url?.trim() || VENDOR_URL
  const expected = (config.sha256?.trim() || VENDOR_SHA256).toLowerCase()
  const exists = deps.exists ?? existsSync
  const mkdir = deps.mkdir ?? ((p: string) => mkdirSync(p, { recursive: true }))
  const writeFile = deps.writeFile ?? ((p: string, d: Uint8Array | string) => writeFileSync(p, d))
  const rm = deps.rm ?? ((p: string) => rmSync(p, { recursive: true, force: true }))
  const chmod = deps.chmod ?? chmodSync
  const paths = vendorPaths(dataDir, expected)

  try {
    mkdir(paths.homeDir)
    // homeDir 底下就是美团的登录 token（`.workbuddy/credentials/...`，由它的 CLI 自己写、默认 0644）。
    // 文件权限追不动（CLI 每次登录重写一遍，追着 chmod 是打补丁），但**目录**是我们建的：0700
    // 一收，同机别的用户就进不来。每次 ensure 都收一遍 = 自愈，不依赖"建的那一次"。
    try {
      chmod(paths.homeDir, 0o700)
    } catch {
      // Windows / 不支持 POSIX 位的文件系统：跳过，不是错。
    }
    // **模块系统的边界墙，必须有。** 美团那个 cliguard 会自升级，把新的 `cliguard.js`（CommonJS）
    // 写进 `<homeDir>/.cliguard/cliguard-updates/core/`，而且不给它配 package.json。Node 判一个
    // `.js` 是 ESM 还是 CJS 是**沿目录向上找最近的 package.json**——找不到就一路走到宿主仓库根，
    // 撞上 Stream 自己那份 `"type":"module"`，于是那个 CJS 文件被当成 ESM 加载：
    // `ReferenceError: require is not defined in ES module scope`，所有子命令全挂。
    // 症状极具迷惑性：**第一次好好的**（用的是包里自带那份），自升级落地之后才开始挂，
    // 而且错误在 stderr 里、stdout 没有 JSON，工具层只能报「code=?」。
    // 这堵墙让向上查找停在 homeDir，宿主仓库怎么声明都碰不到它。
    writeFile(join(paths.homeDir, 'package.json'), '{"type":"commonjs"}\n')
    if (exists(paths.marker) && exists(join(paths.scriptsDir, 'run.js'))) {
      return { ok: true, scriptsDir: paths.scriptsDir, homeDir: paths.homeDir, sha256: expected }
    }

    if (!exists(join(paths.scriptsDir, 'run.js'))) {
      let bytes: Uint8Array
      try {
        const resp = await deps.fetchFn(url)
        if (!resp.ok) return { ok: false, error: `下载美团插件包失败：HTTP ${resp.status}（${url}）`, hint: '美团的公开桶不可达或把这份包撤了；稍后再试，或检查出口网络。' }
        bytes = new Uint8Array(await resp.arrayBuffer())
      } catch (err) {
        return { ok: false, error: `下载美团插件包失败：${err instanceof Error ? err.message : String(err)}`, hint: '检查出口网络；这一步只在第一次用时发生。' }
      }
      const actual = sha256Hex(bytes)
      if (actual !== expected) {
        return { ok: false, error: `上游包变了：期望 sha256 ${expected.slice(0, 12)}…，实得 ${actual.slice(0, 12)}…，拒绝解包`, hint: PIN_HINT }
      }
      mkdir(paths.root)
      const tgz = join(paths.root, 'bundle.tar.gz')
      writeFile(tgz, bytes)
      try {
        await deps.extract(tgz, paths.root)
      } catch (err) {
        rm(paths.root)
        return { ok: false, error: `解包失败：${err instanceof Error ? err.message : String(err)}`, hint: '需要系统里有 tar（Linux / macOS 自带，Windows 10+ 自带 bsdtar）。' }
      }
      if (!exists(join(paths.scriptsDir, 'run.js'))) {
        rm(paths.root)
        return { ok: false, error: '解包后找不到 scripts/run.js——包的布局变了', hint: PIN_HINT }
      }
    }

    const init = await deps.runInit(paths.scriptsDir, paths.homeDir)
    if (!init.ok) {
      return {
        ok: false,
        error: `美团 CLI 初始化失败：${init.error ?? '未知'}`,
        hint: 'run.js init 要 Node ≥18、npm、Python 3 都在 PATH 里（它用 Python 生成设备标识、用 npm 装登录 CLI）。缺哪个装哪个，再调一次即可。',
      }
    }
    writeFile(paths.marker, `${new Date().toISOString()}\n`)
    return { ok: true, scriptsDir: paths.scriptsDir, homeDir: paths.homeDir, sha256: expected }
  } catch (err) {
    return { ok: false, error: `备份美团 CLI 时出错：${err instanceof Error ? err.message : String(err)}`, hint: `检查 ${dataDir} 可写。` }
  }
}
