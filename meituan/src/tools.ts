/**
 * 把美团生活助手的四个动词挂上 DSH 的工具注册表（spec §3）：
 *
 * - `meituan_login`    没登录 → 回授权链接 + 二维码让人扫；`wait:true` → 等扫码结果（≤120s）
 * - `meituan_coupons`  一键领券（写用户美团账号的券）
 * - `meituan_search`   到店团购搜索（按用户近期位置，或给定地址）
 * - `meituan_order`    **真下单、花钱**——由 `tools/pre-execute` 审批门问过用户才跑（`orderGate`）
 *
 * 三条规矩照抄 stream 仓库 `capabilities/netdisk/src/tools.ts`：
 * 1. **绝不抛**。跑在 Stream 后端自己的进程里，逃出去的异常带走的是整个后端。每格收在 `runVerb`
 *    里，失败落成 `{ok:false, error, hint}`，且 **hint 说清下一步**。
 * 2. 判决逻辑不在这里：真正干活的是美团自己的 `run.js`（`cli.ts` 跑它、`vendor.ts` 备它）。
 *    这里只做参数校验、子命令拼装、结果整形、错误码翻译。
 * 3. 这里只**造** `ToolDef`，不负责注册：`meituanToolOptions(deps)` 交回一个数组，由核心
 *    `mount` 递给 `ctx.registerTools`，宿主自己去翻译成它的注册表。所以这个文件（以及整个包）
 *    对宿主是什么零 import、零知晓——它只认 `sdk/capability/types.ts` 那份契约（stream 仓库
 *    `shared/capability/types.ts` 的拷贝，同步规则见 `sdk/README.md`）。
 */
import type { TextBlock, ToolDef } from '../../sdk/capability/types.ts'
import type { CliResult } from './cli.ts'
import type { VendorResult } from './vendor.ts'

export type { TextBlock, ToolDef }

/** `tools/pre-execute` 那道门看到的一次调用（只声明我们读的两格）。 */
export interface ToolExecutionLike {
  name: string
  arguments: unknown
}

export type PreToolDecisionLike = { kind: 'allow' } | { kind: 'deny'; reason: string } | { kind: 'ask'; reason?: string }

// ── 动词体依赖的那几格 ─────────────────────────────────────────────────────────────────

export interface MeituanSurfaceDeps {
  /** 备好美团 CLI（下载 / 校验 / 解包 / init 都在里面；已在场时零成本）。 */
  ensure(): Promise<VendorResult>
  /** 跑一次 `run.js <cmd> …`。`timeoutMs` 由动词按命令给。 */
  run(cmd: string, args: string[], opts: { timeoutMs: number; signal?: AbortSignal }): Promise<CliResult>
  /** 宿主有没有把下单审批门挂上（没有 → `meituan_order` fail closed）。 */
  hasOrderGate(): boolean
}

export interface VerbFailure {
  ok: false
  error: string
  hint?: string
}

export async function runVerb<T>(verb: string, fn: () => Promise<T>): Promise<T | VerbFailure> {
  try {
    return await fn()
  } catch (err) {
    return { ok: false, error: `${verb} 失败：${err instanceof Error ? err.message : String(err)}` }
  }
}

export const TOOL_NAMES = ['meituan_login', 'meituan_coupons', 'meituan_search', 'meituan_order'] as const

/** 普通子命令的超时；`auth-poll-token` 单独给（它是等人扫码的那一个）。 */
const CMD_TIMEOUT_MS = 20_000
const POLL_TIMEOUT_MS = 130_000

const NOT_LOGGED_IN_HINT = '先调 meituan_login 拿授权链接让用户用美团 App 扫码，扫完再调 meituan_login {wait:true} 确认。'

/** 美团接口错误码 → 给模型的一句人话（来源：随包提示词的「场景 G 错误码映射表」）。 */
export function explainMeituanCode(code: unknown, message?: unknown): { error: string; hint: string } {
  const c = typeof code === 'number' ? code : typeof code === 'string' && /^\d+$/.test(code) ? Number(code) : undefined
  const msg = typeof message === 'string' && message.trim() ? `（${message.trim()}）` : ''
  if (c === 401) return { error: `登录已过期${msg}`, hint: NOT_LOGGED_IN_HINT }
  if (c === 509 || c === 50200) return { error: `美团限流：请求过于频繁${msg}`, hint: '稍后再试，别连续重试。' }
  if (c === 403) return { error: `美团拒绝了这次请求（403）${msg}`, hint: '多半是出口 IP 被风控或服务端只放行特定客户端；换网络出口再试，仍 403 就到此为止。' }
  return { error: `美团服务暂时不可用（code=${String(code ?? '?')}）${msg}`, hint: '稍后再试。' }
}

const RENDER_CAP = 60_000

function renderValue(value: unknown): TextBlock[] {
  let text: string
  try {
    text = JSON.stringify(value) ?? String(value)
  } catch {
    text = String(value)
  }
  if (text.length > RENDER_CAP) text = `${text.slice(0, RENDER_CAP)}\n…（结果太长，已截断到 ${RENDER_CAP} 字符）`
  // 这一格**只放 JSON**，一个字都别多。二维码曾经在这里前缀一行 `![二维码](url)`，两头都落空：
  // DSH 的通用工具卡把结果当纯文本画（`white-space: pre-wrap`），markdown 原样显示成一行字；
  // 而工作台的定制卡是 `JSON.parse` 这段文本拿数据的，前缀一加就 parse 不出来 —— 卡片当场变成
  // 「失败」并把整段原文吐出来（活体 2026-09-04 撞到：登录明明成功、拿到了码，用户看到的是报错）。
  // 二维码归渲染层：`dsh-plugin-stream-ui` 的 MeituanQrCard 从 qrImageUrl/payQrImageUrl 画 <img>。
  return [{ type: 'text', text }]
}

const OUTPUT = {
  schema: { type: 'json' } as const,
  render: (_args: unknown, value: unknown): TextBlock[] => renderValue(value),
}

const str = (v: unknown): string | undefined => (typeof v === 'string' && v.trim().length > 0 ? v.trim() : undefined)
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() && Number.isFinite(Number(v)) ? Number(v) : undefined)

function requireStr(a: Record<string, unknown>, key: string, what: string): string {
  const s = str(a[key])
  if (!s) throw new Error(`缺 ${key}——${what}`)
  return s
}

/**
 * 一次 run.js 调用：CLI 层失败 → 直接抛（runVerb 收）；拿到 JSON 就交给调用方判 `ok`。
 *
 * 多一道「把真话捞出来」：run.js 自己吃掉子进程异常时会回
 * `{ok:false, error:'UNKNOWN', stderr:'<真正的报错>'}` —— 既没有 `code` 也没有 `message`，
 * 于是下游的错误映射只能说"美团服务暂时不可用（code=?）"，把唯一有用的那行埋掉。
 * 活体撞过一次（cliguard 自升级出来的 CJS 撞上宿主 `type:module`，全部子命令挂掉，报的却是
 * 「服务不可用」，看起来像美团那边的问题）。这里把 stderr 的尾巴折进 `message`。
 */
async function cli(deps: MeituanSurfaceDeps, cmd: string, args: string[], signal: AbortSignal | undefined, timeoutMs = CMD_TIMEOUT_MS): Promise<Record<string, unknown>> {
  const r = await deps.run(cmd, args, signal ? { timeoutMs, signal } : { timeoutMs })
  if (!r.ok) throw new Error(r.error)
  const j = r.json
  if (j.ok === false && j.message === undefined && j.code === undefined) {
    const detail = typeof j.stderr === 'string' ? j.stderr : ''
    const tail = detail.split(/\r?\n/).map((l) => l.trim()).filter((l) => l !== '' && l.length < 300).slice(-2).join(' | ')
    if (tail !== '') return { ...j, message: `run.js ${cmd} 内部报错：${tail}` }
  }
  return j
}

/** 备好 CLI；失败原样回给模型（error + hint）。 */
async function ready(deps: MeituanSurfaceDeps): Promise<VerbFailure | undefined> {
  const v = await deps.ensure()
  return v.ok ? undefined : { ok: false, error: v.error, hint: v.hint }
}

/** 有没有有效 token（`get-token`）。 */
async function loggedIn(deps: MeituanSurfaceDeps, signal?: AbortSignal): Promise<boolean> {
  const j = await cli(deps, 'get-token', [], signal)
  return j.ok === true && typeof j.token === 'string' && j.token.length > 0
}

/** 团购商品行：只保留标量字段（productList 每条是一大坨嵌套对象，模型只要能报名字、价格、拿 id 下单）。 */
export function trimProduct(item: unknown): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {}
  if (!item || typeof item !== 'object') return out
  for (const [k, v] of Object.entries(item as Record<string, unknown>)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') out[k] = v
  }
  return out
}

/**
 * 下单审批门（spec §3）：命中 `meituan_order` → `ask`，由 `ctx.approval` 问用户；其它一律放行
 * （返回 undefined = 调用方 `next()`）。确认文案从参数里拼——`title / poiName / price` 三个参数
 * 就是为它而存在的，不参与下单请求。
 */
export function orderGate(exec: ToolExecutionLike): PreToolDecisionLike | undefined {
  if (exec.name !== 'meituan_order') return undefined
  const a = (exec.arguments && typeof exec.arguments === 'object' ? exec.arguments : {}) as Record<string, unknown>
  const qty = num(a.quantity) ?? 1
  const price = num(a.price)
  const parts = [`美团下单：${str(a.poiName) ?? '（店名未知）'}《${str(a.title) ?? '（商品未知）'}》×${qty}`]
  if (price !== undefined) parts.push(`单价 ¥${price}`)
  parts.push('确认后会真的在你的美团账号里生成订单并返回支付链接。')
  return { kind: 'ask', reason: parts.join('，') }
}

/**
 * 造出四个动词的 `defineTool` 入参。抽成纯函数是为了不接真宿主也能把整套测到。
 */
export function meituanToolOptions(deps: MeituanSurfaceDeps): ToolDef[] {
  const make = (
    name: string,
    description: string,
    parameters: ToolDef['parameters'],
    execute: (a: Record<string, unknown>, signal?: AbortSignal) => Promise<unknown>,
    annotations?: ToolDef['annotations'],
  ): ToolDef => ({
    name,
    description,
    parameters,
    output: OUTPUT,
    execute: (a, exec) => runVerb(name, () => execute(a, exec?.signal)),
    ...(annotations ? { annotations } : {}),
  })

  return [
    make(
      'meituan_login',
      '美团账号登录状态。不带参数：已登录就回 loggedIn:true；没登录就回一个授权链接 authUrl 和二维码 qrImageUrl（10 分钟内有效），把它们原样给用户、让用户用美团 App 扫码或点链接。用户说扫完了之后，带 wait:true 再调一次：它会等授权结果（最多约两分钟），回 loggedIn:true 或 pending（还没扫完，可以再等一次）。登录态存在本机插件目录里，之后 30 天内不用重扫。',
      {
        wait: { type: 'boolean', description: 'true = 等用户扫码的结果（阻塞最多约两分钟）；省略 = 只查状态 / 出授权链接' },
      },
      async (a, signal) => {
        const notReady = await ready(deps)
        if (notReady) return notReady
        if (a.wait === true) {
          const j = await cli(deps, 'auth-poll-token', [], signal, POLL_TIMEOUT_MS)
          if (j.ok === true) return { ok: true, loggedIn: true }
          return { ok: true, loggedIn: false, status: 'pending', hint: `${String(j.message ?? '还没拿到授权结果')}——用户扫完了就再调一次 meituan_login {wait:true}；链接过期就不带 wait 重新拿一个。` }
        }
        if (await loggedIn(deps, signal)) return { ok: true, loggedIn: true }
        const code = await cli(deps, 'auth-get-code', [], signal)
        if (code.ok === true && code.type === 'token') return { ok: true, loggedIn: true }
        if (code.ok !== true || code.type !== 'auth_link' || typeof code.url !== 'string') {
          const ex = explainMeituanCode(code.code, code.message)
          return { ok: false, error: `拿不到美团授权链接：${ex.error}`, hint: ex.hint }
        }
        const authUrl = code.url
        let qrImageUrl: string | undefined
        try {
          const qr = await cli(deps, 'qrcode', [authUrl], signal)
          if (qr.ok === true && typeof qr.imageUrl === 'string') qrImageUrl = qr.imageUrl
        } catch {
          /* 二维码只是方便扫；没有就给链接 */
        }
        return {
          ok: true,
          loggedIn: false,
          authUrl,
          ...(qrImageUrl ? { qrImageUrl } : {}),
          expiresInMin: 10,
          next: '把二维码/链接给用户，让他用美团 App 扫码或点开并确认授权；用户说完成后调 meituan_login {wait:true}。',
        }
      },
    ),
    make(
      'meituan_coupons',
      '一键领取美团各品类优惠券（外卖红包、餐饮团购、酒店、门票、闪购、买药等），领到的券进用户自己的美团账号（美团 App「我的 → 优惠券」）。要先登录。当天已经领过会回 cached:true 和当天那份券表；没有可领的券回 claimed:false。',
      {},
      async (_a, signal) => {
        const notReady = await ready(deps)
        if (notReady) return notReady
        if (!(await loggedIn(deps, signal))) return { ok: false, error: '还没登录美团', hint: NOT_LOGGED_IN_HINT }
        const j = await cli(deps, 'issue', [], signal)
        const coupons = Array.isArray(j.display_coupons) ? j.display_coupons : Array.isArray(j.coupons) ? j.coupons : []
        const count = num(j.coupon_count) ?? coupons.length
        if (j.cached === true) return { ok: true, claimed: false, cached: true, count, countStr: str(j.count_str) ?? '', coupons }
        if (j.success === true && count > 0) return { ok: true, claimed: true, cached: false, count, countStr: str(j.count_str) ?? '', coupons }
        if (num(j.code) === 1014) return { ok: true, claimed: false, cached: false, count: 0, countStr: '', coupons: [], note: '当前美团暂无可领的优惠券' }
        const ex = explainMeituanCode(j.code, j.message)
        return { ok: false, error: `领券失败：${ex.error}`, hint: ex.hint }
      },
    ),
    make(
      'meituan_search',
      '搜附近的美团到店团购（餐饮/饮品/咖啡/奶茶/火锅/烧烤/日料/自助餐等团购券；不含外卖、酒旅）。不给 address 就按用户在美团上的近期位置；给了 address（如「上海青浦华新镇」）就按那个地址定位。返回商品列表（每条带 productId / poiId，下单要用）以及这次定位到的 cityId / lat / lng（下单也要带上）。要先登录。',
      {
        keyword: { type: 'string', required: true, description: '想吃什么或哪家店：「火锅」「海底捞」「下午茶」' },
        address: { type: 'string', description: '在哪附近找；省略 = 用户在美团上的近期位置' },
        page: { type: 'number', description: '第几页，从 1 起；省略 = 1' },
        max_distance_km: { type: 'number', description: '最远多少公里；省略 = 8' },
      },
      async (a, signal) => {
        const keyword = requireStr(a, 'keyword', '想搜什么')
        const notReady = await ready(deps)
        if (notReady) return notReady
        if (!(await loggedIn(deps, signal))) return { ok: false, error: '还没登录美团', hint: NOT_LOGGED_IN_HINT }
        const address = str(a.address)
        const loc = address ? await cli(deps, 'location-by-address', ['--address', address], signal) : await cli(deps, 'location', [], signal)
        if (loc.ok !== true) {
          const ex = explainMeituanCode(loc.code, loc.error ?? loc.message)
          return { ok: false, error: `定位失败：${ex.error}`, hint: address ? '换个更具体的地址（区 + 路名 / 小区名）。' : '用户在美团上没有近期位置；让用户说一个地址，带 address 再试。' }
        }
        const cityId = String(loc.cityId ?? '')
        const lat = String(loc.lat ?? '')
        const lng = String(loc.lng ?? '')
        if (!cityId || !lat || !lng) return { ok: false, error: '定位结果不完整（缺 cityId/lat/lng）', hint: '带一个更具体的 address 再试。' }
        const args = ['--keyword', keyword, '--lat', lat, '--lng', lng, '--city-id', cityId]
        const page = num(a.page)
        if (page && page > 1) args.push('--page', String(Math.floor(page)))
        const maxKm = num(a.max_distance_km)
        if (maxKm && maxKm > 0) args.push('--max-distance-km', String(maxKm))
        const j = await cli(deps, 'search', args, signal)
        if (j.ok !== true) {
          const ex = explainMeituanCode(j.code, j.message ?? j.error)
          return { ok: false, error: `搜索失败：${ex.error}`, hint: ex.hint }
        }
        const products = (Array.isArray(j.productList) ? j.productList : []).map(trimProduct)
        return {
          ok: true,
          location: { cityId, cityName: str(loc.cityName) ?? '', address: str(loc.formattedAddress) ?? address ?? '', lat, lng },
          page: num(j.page) ?? page ?? 1,
          isLastPage: j.isLastPage === true,
          products,
          note: products.length ? undefined : '这一带没搜到；换个关键词或放宽 max_distance_km。',
        }
      },
    ),
    make(
      'meituan_order',
      '会真下单、真扣款；在用户的美团账号里真的下一单到店团购（**花钱**）。参数来自 meituan_search 的结果：productId / poiId 取自商品行，cityId / lat / lng 取自那次搜索的 location。title / poiName / price 三个只用来向用户确认（照搜索结果原样填），不参与下单。调用会先弹审批让用户确认；确认后返回订单号和支付链接 payUrl / 支付二维码 payQrImageUrl，把它们给用户去付款——插件不代付。',
      {
        productId: { type: 'string', required: true, description: '商品 id（搜索结果里的 productId）' },
        poiId: { type: 'string', required: true, description: '门店 id（搜索结果里的 poiId）' },
        cityId: { type: 'string', required: true, description: '那次搜索 location.cityId' },
        lat: { type: 'string', description: '那次搜索 location.lat' },
        lng: { type: 'string', description: '那次搜索 location.lng' },
        quantity: { type: 'number', description: '份数；省略 = 1' },
        title: { type: 'string', required: true, description: '商品名（用于向用户确认）' },
        poiName: { type: 'string', required: true, description: '店名（用于向用户确认）' },
        price: { type: 'number', description: '单价，元（用于向用户确认）' },
      },
      async (a, signal) => {
        const productId = requireStr(a, 'productId', '商品 id')
        const poiId = requireStr(a, 'poiId', '门店 id')
        const cityId = requireStr(a, 'cityId', '城市 id')
        requireStr(a, 'title', '商品名，用于向用户确认')
        requireStr(a, 'poiName', '店名，用于向用户确认')
        if (!deps.hasOrderGate()) {
          return { ok: false, error: '宿主没有挂上下单审批门（tools/pre-execute），拒绝下单', hint: '这是 fail-closed：没有人来问用户就不花钱。让宿主用真 cordis Context 装载本插件。' }
        }
        const notReady = await ready(deps)
        if (notReady) return notReady
        if (!(await loggedIn(deps, signal))) return { ok: false, error: '还没登录美团', hint: NOT_LOGGED_IN_HINT }
        const dev = await cli(deps, 'get-device-token', [], signal)
        const uuid = str(dev.device_token)
        if (!uuid) return { ok: false, error: '拿不到设备标识（get-device-token）', hint: 'run.js 用 Python 3 生成它；确认 python3 在 PATH 里。' }
        const args = ['--product-id', productId, '--poi-id', poiId, '--city-id', cityId, '--uuid', uuid]
        const lat = str(a.lat)
        const lng = str(a.lng)
        if (lat) args.push('--lat', lat)
        if (lng) args.push('--lng', lng)
        const qty = num(a.quantity)
        if (qty && qty > 1) args.push('--quantity', String(Math.floor(qty)))
        const j = await cli(deps, 'order', args, signal)
        if (j.ok !== true) {
          const ex = explainMeituanCode(j.code, j.message ?? j.error)
          return { ok: false, error: `下单失败：${ex.error}`, hint: ex.hint }
        }
        const payUrl = str(j.payShortLink)
        const payQrImageUrl = str(j.payQrCodeImage)
        return {
          ok: true,
          orderId: str(j.orderId) ?? '',
          ...(payUrl ? { payUrl } : {}),
          ...(payQrImageUrl ? { payQrImageUrl } : {}),
          next: '把支付链接 / 二维码给用户去付款；未付款的订单会在美团那边超时自动取消。',
        }
      },
      // 四个动词里只有这一个会花钱。`destructiveHint` 是给宿主看的：Claude Code 据它默认不把这条
      // 放进「总是允许」，DSH 走 DSH 脸里挂的 `orderGate`。核心自己不认识任何一种确认机制。
      { destructiveHint: true },
    ),
  ]
}
