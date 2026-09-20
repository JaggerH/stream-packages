# @streamapp/meituan

Stream 的**可选能力包**：**美团生活**——领券 / 到店团购搜索 / 下单，走的是美团**官方**给 WorkBuddy
发的那份「美团生活助手」CLI（登录、签名、接口全是美团自己的），我们只是把它物化到本机、钉死哈希、
包成四个给模型的动词。

这份 README 是这个包对外的全部说明，所以它**自包含**：不指向仓库里的设计文档（那些路径装了包
的人一个都打不开）。

## 装法

```bash
stream add @streamapp/meituan
```

装进 `<dataDir>/recipes/@streamapp__meituan/`，Stream 后端**重载后**按
`package.json#stream.capability` 这一格动态 import `dist/index.js`、取它导出的 `capability`
挂上。四个动词随之出现在 8900 的 `/api/mcp` 上，宿主（Claude Code / Codex / DSH）那一行不用
改——它们只认得 Stream 一个口。不想要了就 `stream remove @streamapp/meituan`。

**不申报 `credentials`**：它不借浏览器登录态，登录是美团 CLI 自己那套扫码，token 落在这个包
自己的 `<dataDir>/home` 里（下面「美团的字节从哪来」）。

## 给模型的四个动词

| 动词 | 做什么 | 副作用 |
|---|---|---|
| `meituan_login` | 出授权链接 + 二维码让用户用美团 App 扫；`wait:true` 等扫码结果 | 无 |
| `meituan_coupons` | 一键领全品类优惠券 | 券进用户的美团账号 |
| `meituan_search` | 到店团购搜索（按用户近期位置，或给定 `address`） | 无 |
| `meituan_order` | 真下单，回支付链接 / 二维码 | **花钱** |

**只支持到店团购，不支持外卖 / 酒旅。** 支付由用户自己扫码完成，本包不代付。

**下单前问不问人，由宿主决定，不由包自己两步走**（模型能自己连调两步，那是假安全）。包只做一件
事：`meituan_order` 标 `annotations.destructiveHint: true`，description 首句写明会真下单、真扣款。
Stream 把 `ctx.destructiveGate` 报成 `'host'`——`destructiveHint` 原样透传给 MCP，由宿主
（Claude Code / Codex / DSH）自己弹确认；宿主报 `'none'` 时本包 fail closed，直接不跑。

## 美团的字节从哪来

不在这个 npm 包里。第一次调任何动词时，本包从美团的公开桶下载 `meituan-living-assistant.tar.gz`，
校验 `src/vendor.ts` 里钉死的 sha256，解到 `<dataDir>/vendor/<sha 前 12 位>/`，再跑它自己的
`scripts/run.js init`（把美团的 `pt-passport` 登录 CLI 装进 `scripts/node_modules`）。

- **哈希不对就停**：那份包里有混淆过的登录 CLI 和自升级守护进程，换了就得有人重新读一遍再改 pin。
- 子进程的 `HOME` 指到 `<dataDir>/home`：美团 CLI 写的 token（`~/.workbuddy/credentials/…`）和
  cliguard 的自升级缓存（`~/.cliguard`）全落在本包的 data 目录里，不碰用户真正的 home。
- 要 Node ≥18、npm、Python 3 在 PATH 里（`run.js init` 自己检查，缺哪个动词就报哪个）。

## 这个包的 config

从 Stream 的 `config.yaml` 里 `capabilities.meituan` 那一格来，全部可省：

```yaml
capabilities:
  meituan:
    dataDir: /path/to/data/meituan-plugin      # 缺省 <Stream dataDir>/capabilities/meituan
    vendor:                                     # 升级美团包、人核过之后才改
      url: https://…/meituan-living-assistant.tar.gz
      sha256: ace91900…
```

## 排错

- 「上游包变了」= 美团换了包。下载新包、逐文件读过（尤其 `scripts/run.js` 和 `auth.py`）再更新
  `VENDOR_SHA256`。
- 领券 / 搜索回 403 → 出口 IP 被风控，或服务端只放行特定客户端。随包 `references/DOCTOR.md` 的判据：
  `HEAD https://media.meituan.com/fulishemini/couponActivity/sendCouponByAi` 回 403 = 风控，405 = 正常。
- 401 = 登录过期，重走 `meituan_login`。登录态 30 天有效。
- 下单被拒「宿主没有挂上下单审批门」= 装载它的宿主报了 `destructiveGate: 'none'`。
  fail closed，故意的：拦不住就不跑。

## 给模型的那一段说法

下面两个标记之间的英文是这套工具的人格段：要把它喂进某个宿主的系统提示词/preset，从这里取，
别手写第二份（与 purchase-decision / netdisk-library 两个 skill 同一约定）。

<!-- persona:start -->
Meituan (领券 / 到店团购 / 下单) — the `meituan_*` tools. When the user asks for Meituan coupons, deals, or "what's good to eat nearby", first call `meituan_login`; if it returns an auth link and a QR image, a host that renders tool cards shows that QR as an image in the card itself — tell the user to scan the QR shown in the card with the Meituan app (paste the `authUrl` link as a fallback for phones), then call `meituan_login` with `wait:true` once they say it is done. `meituan_coupons` claims every available coupon in one call — present the returned table and say the coupons are in the Meituan app under 我的 → 优惠券; if it returns `cached:true`, say today's coupons were already claimed. `meituan_search` finds dine-in group-buy deals near the user's recent location (or a given `address`) and returns `productId`/`poiId` per item plus the `location` it used — keep those, ordering needs them. `meituan_order` places a REAL order and costs money: only call it after the user has picked a specific item, pass `title`/`poiName`/`price` from the search result so the approval prompt can name it, and then point the user at the payment QR the tool card shows (paste `payUrl` as the phone fallback) — you never pay on their behalf. Delivery (外卖), hotels and travel are outside these tools; say so instead of improvising.
<!-- persona:end -->
