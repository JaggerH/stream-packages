// meituan/src/tools.test.ts
//
// 四个动词的「名字 / 参数表 / 子命令拼装 / 结果整形 / 错误翻译」整套不接真宿主、不跑一行美团代码
// 就能测：`run` 是按子命令分流的假 CLI。每一格都**绝不抛**。
import { describe, it, expect } from 'vitest'
import { explainMeituanCode, meituanToolOptions, orderGate, trimProduct, type MeituanSurfaceDeps } from './tools.ts'
import type { CliResult } from './cli.ts'

type Script = Record<string, (args: string[]) => Record<string, unknown> | { cliError: string }>

function fakeDeps(script: Script, o: { ensureFails?: boolean; gate?: boolean } = {}) {
  const calls: Array<{ cmd: string; args: string[]; timeoutMs: number }> = []
  const deps: MeituanSurfaceDeps = {
    ensure: async () => (o.ensureFails ? { ok: false, error: '上游包变了', hint: 'pin' } : { ok: true, scriptsDir: '/v/scripts', homeDir: '/v/home', sha256: 'abc' }),
    run: async (cmd, args, opts): Promise<CliResult> => {
      calls.push({ cmd, args, timeoutMs: opts.timeoutMs })
      const fn = script[cmd]
      if (!fn) return { ok: false, error: `unscripted ${cmd}`, stdout: '', stderr: '' }
      const r = fn(args)
      if ('cliError' in r) return { ok: false, error: String(r.cliError), stdout: '', stderr: '' }
      return { ok: true, json: r, stdout: '', stderr: '' }
    },
    hasOrderGate: () => o.gate !== false,
  }
  return { deps, calls }
}

const verb = (deps: MeituanSurfaceDeps, name: string) => {
  const hit = meituanToolOptions(deps).find((t) => t.name === name)
  if (!hit) throw new Error(`no verb ${name}`)
  return hit
}

const LOGGED_IN: Script = { 'get-token': () => ({ ok: true, token: 'T' }) }
const LOGGED_OUT: Script = { 'get-token': () => ({ ok: false }) }

describe('meituanToolOptions —— 名字与参数表', () => {
  it('恰好四个动词，每个参数都带描述', () => {
    const opts = meituanToolOptions(fakeDeps({}).deps)
    expect(opts.map((o) => o.name).sort()).toEqual(['meituan_coupons', 'meituan_login', 'meituan_order', 'meituan_search'])
    for (const o of opts) {
      expect(o.description.length).toBeGreaterThan(30)
      for (const [k, p] of Object.entries(o.parameters)) expect((p as { description?: string }).description, `${o.name}.${k}`).toBeTruthy()
    }
  })
  it('下单的说明书第一句就说它会真下单、真扣款，且写明会先弹审批', () => {
    const d = verb(fakeDeps({}).deps, 'meituan_order').description
    expect(d.startsWith('会真下单、真扣款；')).toBe(true)
    expect(d).toMatch(/花钱/)
    expect(d).toMatch(/审批/)
  })
  it('只有 meituan_order 带 destructiveHint，其余三个不带', () => {
    const opts = meituanToolOptions(fakeDeps({}).deps)
    expect(opts.filter((o) => o.annotations?.destructiveHint === true).map((o) => o.name)).toEqual(['meituan_order'])
    for (const o of opts) {
      if (o.name !== 'meituan_order') expect(o.annotations, o.name).toBeUndefined()
    }
  })
})

describe('meituan_login', () => {
  it('有 token → loggedIn:true，不去拿授权链接', async () => {
    const f = fakeDeps(LOGGED_IN)
    expect(await verb(f.deps, 'meituan_login').execute({})).toEqual({ ok: true, loggedIn: true })
    expect(f.calls.map((c) => c.cmd)).toEqual(['get-token'])
  })
  it('没 token → auth-get-code 出链接 + qrcode 出图，回 authUrl / qrImageUrl', async () => {
    const f = fakeDeps({
      ...LOGGED_OUT,
      'auth-get-code': () => ({ ok: true, type: 'auth_link', url: 'https://npay.meituan.com/x' }),
      qrcode: (args) => ({ ok: true, type: 'image', imageUrl: `https://img/${encodeURIComponent(args[0]!)}` }),
    })
    const r = (await verb(f.deps, 'meituan_login').execute({})) as Record<string, unknown>
    expect(r).toMatchObject({ ok: true, loggedIn: false, authUrl: 'https://npay.meituan.com/x', expiresInMin: 10 })
    expect(r.qrImageUrl).toContain('npay.meituan.com')
    // 渲染这一格必须是**纯 JSON**：工作台的定制卡靠 JSON.parse 它拿 qrImageUrl 画 <img>，
    // 前面多一行 markdown 就 parse 不出来、整张卡变「失败」（活体撞过）。
    const text = verb(f.deps, 'meituan_login').output.render({}, r)[0]!.text
    expect(JSON.parse(text)).toMatchObject({ ok: true, qrImageUrl: r.qrImageUrl })
  })
  it('run.js 自己挂了（ok:false 但只有 stderr）→ 报错里带上真正的那行，不是「code=?」', async () => {
    // 活体撞过：cliguard 自升级出来的 CJS 撞上宿主的 type:module，全部子命令挂掉，
    // 而用户看到的是"美团服务暂时不可用（code=?）"——唯一有用的那行躺在 stderr 里被丢了。
    const f = fakeDeps({
      ...LOGGED_OUT,
      'auth-get-code': () => ({ ok: false, error: 'UNKNOWN', raw: '', stderr: 'ReferenceError: require is not defined in ES module scope\n    at foo' }),
    })
    const r = (await verb(f.deps, 'meituan_login').execute({})) as Record<string, unknown>
    expect(r.ok).toBe(false)
    expect(String(r.error)).toContain('require is not defined')
  })

  it('二维码生成失败 → 仍回链接（二维码只是方便）', async () => {
    const f = fakeDeps({ ...LOGGED_OUT, 'auth-get-code': () => ({ ok: true, type: 'auth_link', url: 'https://u' }), qrcode: () => ({ cliError: 'boom' }) })
    const r = (await verb(f.deps, 'meituan_login').execute({})) as Record<string, unknown>
    expect(r).toMatchObject({ ok: true, loggedIn: false, authUrl: 'https://u' })
    expect(r.qrImageUrl).toBeUndefined()
  })
  it('wait:true → auth-poll-token（长超时）；成功 loggedIn，失败 pending 且不抛', async () => {
    const ok = fakeDeps({ 'auth-poll-token': () => ({ ok: true, token: 'T' }) })
    expect(await verb(ok.deps, 'meituan_login').execute({ wait: true })).toEqual({ ok: true, loggedIn: true })
    expect(ok.calls[0]!.timeoutMs).toBeGreaterThan(100_000)
    const no = fakeDeps({ 'auth-poll-token': () => ({ ok: false, message: '登录失败，请重新登录' }) })
    expect(await verb(no.deps, 'meituan_login').execute({ wait: true })).toMatchObject({ ok: true, loggedIn: false, status: 'pending' })
  })
  it('vendor 备不好 → 原样把 error/hint 交给模型', async () => {
    const f = fakeDeps(LOGGED_IN, { ensureFails: true })
    expect(await verb(f.deps, 'meituan_login').execute({})).toEqual({ ok: false, error: '上游包变了', hint: 'pin' })
  })
})

describe('meituan_coupons', () => {
  it('没登录 → 不调 issue，指路 meituan_login', async () => {
    const f = fakeDeps(LOGGED_OUT)
    const r = (await verb(f.deps, 'meituan_coupons').execute({})) as { ok: boolean; hint?: string }
    expect(r.ok).toBe(false)
    expect(r.hint).toMatch(/meituan_login/)
    expect(f.calls.some((c) => c.cmd === 'issue')).toBe(false)
  })
  it('领到券 → claimed:true + 券表', async () => {
    const f = fakeDeps({ ...LOGGED_IN, issue: () => ({ ok: true, success: true, coupon_count: 2, count_str: '外卖券2张', display_coupons: [{ name: '满40减20' }, { name: '满30减14' }] }) })
    expect(await verb(f.deps, 'meituan_coupons').execute({})).toMatchObject({ ok: true, claimed: true, cached: false, count: 2, countStr: '外卖券2张' })
  })
  it('当天领过（cached）→ claimed:false cached:true 带当天那份', async () => {
    const f = fakeDeps({ ...LOGGED_IN, issue: () => ({ ok: false, code: 1014, cached: true, coupon_count: 1, display_coupons: [{ name: 'x' }] }) })
    expect(await verb(f.deps, 'meituan_coupons').execute({})).toMatchObject({ ok: true, claimed: false, cached: true, count: 1 })
  })
  it('1014 且非 cached → 无可领券', async () => {
    const f = fakeDeps({ ...LOGGED_IN, issue: () => ({ ok: false, success: false, code: 1014 }) })
    expect(await verb(f.deps, 'meituan_coupons').execute({})).toMatchObject({ ok: true, claimed: false, count: 0 })
  })
  it('401 → 登录过期 + 指路重登；509 → 限流', async () => {
    const a = fakeDeps({ ...LOGGED_IN, issue: () => ({ ok: false, success: false, code: 401 }) })
    expect(await verb(a.deps, 'meituan_coupons').execute({})).toMatchObject({ ok: false, error: expect.stringMatching(/过期/), hint: expect.stringMatching(/meituan_login/) })
    const b = fakeDeps({ ...LOGGED_IN, issue: () => ({ ok: false, success: false, code: 509 }) })
    expect(await verb(b.deps, 'meituan_coupons').execute({})).toMatchObject({ ok: false, error: expect.stringMatching(/限流/) })
  })
  it('CLI 层挂了（没 JSON）→ 不抛，ok:false', async () => {
    const f = fakeDeps({ ...LOGGED_IN, issue: () => ({ cliError: 'run.js issue 没有输出 JSON' }) })
    const r = (await verb(f.deps, 'meituan_coupons').execute({})) as { ok: boolean; error: string }
    expect(r.ok).toBe(false)
    expect(r.error).toMatch(/没有输出 JSON/)
  })
})

describe('meituan_search', () => {
  const LOC = { ok: true, cityId: 10, cityName: '上海', lat: '31.2', lng: '121.1', formattedAddress: '青浦区华新镇' }
  const HIT = { ok: true, productList: [{ index: 1, productId: 'p1', poiId: 's1', title: '双人火锅套餐', price: 128, distanceText: '1.2km', nested: { a: 1 } }], isLastPage: true, page: 1 }
  it('不给 address → location；拼 search 参数；商品只留标量字段', async () => {
    const f = fakeDeps({ ...LOGGED_IN, location: () => LOC, search: () => HIT })
    const r = (await verb(f.deps, 'meituan_search').execute({ keyword: '火锅' })) as Record<string, unknown>
    expect(r).toMatchObject({ ok: true, location: { cityId: '10', cityName: '上海', lat: '31.2', lng: '121.1' }, isLastPage: true })
    expect((r.products as unknown[])[0]).toEqual({ index: 1, productId: 'p1', poiId: 's1', title: '双人火锅套餐', price: 128, distanceText: '1.2km' })
    const s = f.calls.find((c) => c.cmd === 'search')!
    expect(s.args).toEqual(['--keyword', '火锅', '--lat', '31.2', '--lng', '121.1', '--city-id', '10'])
  })
  it('给 address → location-by-address；page / max_distance_km 透传', async () => {
    const f = fakeDeps({ ...LOGGED_IN, 'location-by-address': (args) => ({ ok: true, cityId: 1, lat: '1', lng: '2', addr: args[1] }), search: () => HIT })
    await verb(f.deps, 'meituan_search').execute({ keyword: '咖啡', address: '陆家嘴', page: 2, max_distance_km: 3 })
    expect(f.calls.find((c) => c.cmd === 'location-by-address')!.args).toEqual(['--address', '陆家嘴'])
    expect(f.calls.find((c) => c.cmd === 'search')!.args.slice(-4)).toEqual(['--page', '2', '--max-distance-km', '3'])
  })
  it('缺 keyword → ok:false 不抛；定位失败 → 让用户给地址', async () => {
    const f = fakeDeps({ ...LOGGED_IN, location: () => ({ ok: false, error: '无近期位置', code: 500 }) })
    expect(await verb(f.deps, 'meituan_search').execute({})).toMatchObject({ ok: false, error: expect.stringMatching(/keyword/) })
    expect(await verb(f.deps, 'meituan_search').execute({ keyword: 'x' })).toMatchObject({ ok: false, error: expect.stringMatching(/定位失败/), hint: expect.stringMatching(/address/) })
  })
})

describe('meituan_order', () => {
  const ARGS = { productId: 'p1', poiId: 's1', cityId: '10', lat: '31.2', lng: '121.1', quantity: 2, title: '双人火锅套餐', poiName: '海底捞', price: 128 }
  it('有审批门 + 已登录 → get-device-token 后 order，回订单号与支付链接/二维码', async () => {
    const f = fakeDeps({ ...LOGGED_IN, 'get-device-token': () => ({ ok: true, device_token: 'dev-1' }), order: () => ({ ok: true, orderId: 'o9', payShortLink: 'https://pay/x', payQrCodeImage: 'https://qr/x' }) })
    const r = await verb(f.deps, 'meituan_order').execute(ARGS)
    expect(r).toMatchObject({ ok: true, orderId: 'o9', payUrl: 'https://pay/x', payQrImageUrl: 'https://qr/x' })
    expect(f.calls.find((c) => c.cmd === 'order')!.args).toEqual(['--product-id', 'p1', '--poi-id', 's1', '--city-id', '10', '--uuid', 'dev-1', '--lat', '31.2', '--lng', '121.1', '--quantity', '2'])
    expect(JSON.parse(verb(f.deps, 'meituan_order').output.render({}, r)[0]!.text)).toMatchObject({ payQrImageUrl: 'https://qr/x' })
  })
  it('宿主没挂审批门 → fail closed，一个子命令都不跑', async () => {
    const f = fakeDeps(LOGGED_IN, { gate: false })
    const r = (await verb(f.deps, 'meituan_order').execute(ARGS)) as { ok: boolean; error: string }
    expect(r.ok).toBe(false)
    expect(r.error).toMatch(/审批门/)
    expect(f.calls).toHaveLength(0)
  })
  it('缺 title/poiName（确认文案要用）→ 拒绝', async () => {
    const f = fakeDeps(LOGGED_IN)
    expect(await verb(f.deps, 'meituan_order').execute({ productId: 'p', poiId: 's', cityId: '1' })).toMatchObject({ ok: false, error: expect.stringMatching(/title/) })
  })
  it('下单被拒（如 403）→ 说清出口风控的可能', async () => {
    const f = fakeDeps({ ...LOGGED_IN, 'get-device-token': () => ({ ok: true, device_token: 'd' }), order: () => ({ ok: false, code: 403, message: 'forbidden' }) })
    expect(await verb(f.deps, 'meituan_order').execute(ARGS)).toMatchObject({ ok: false, error: expect.stringMatching(/403/), hint: expect.stringMatching(/风控|客户端/) })
  })
})

describe('orderGate（tools/pre-execute）', () => {
  it('meituan_order → ask，文案里有店名、商品、份数、单价', () => {
    const d = orderGate({ name: 'meituan_order', arguments: { poiName: '海底捞', title: '双人套餐', quantity: 2, price: 128 } })
    expect(d).toMatchObject({ kind: 'ask' })
    expect((d as { reason: string }).reason).toMatch(/海底捞.*双人套餐.*×2.*¥128/)
  })
  it('参数残缺也照样 ask（不因为文案拼不全就放行）', () => {
    expect(orderGate({ name: 'meituan_order', arguments: null })).toMatchObject({ kind: 'ask' })
  })
  it('其它工具 → undefined（调用方 next()）', () => {
    expect(orderGate({ name: 'meituan_coupons', arguments: {} })).toBeUndefined()
    expect(orderGate({ name: 'bash', arguments: {} })).toBeUndefined()
  })
})

describe('explainMeituanCode / trimProduct', () => {
  it('未知码 → 通用话术；字符串数字也认', () => {
    expect(explainMeituanCode('401').hint).toMatch(/meituan_login/)
    expect(explainMeituanCode(2213, '服务开小差').error).toMatch(/2213/)
  })
  it('trimProduct 丢掉嵌套、保留标量', () => {
    expect(trimProduct({ a: 1, b: 'x', c: true, d: { e: 1 }, f: [1] })).toEqual({ a: 1, b: 'x', c: true })
    expect(trimProduct(null)).toEqual({})
  })
})
