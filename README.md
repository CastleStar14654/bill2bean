# bill2bean

把支付宝 CSV、微信支付 xlsx、工商银行信用卡对账单邮件转换成可人工核对的 CSV，再导出 Beancount。

## 用法

生成核对表：

```bash
python3 -m bill2bean.cli review -c config.toml -o review.csv \
  '支付宝交易明细(20260601-20260618).csv' \
  '微信支付账单流水文件20260601_20260618_20260618172913.xlsx' \
  '中国工商银行客户对账单2026-06-18.eml'
```

如果已经有上一次人工编辑过的核对表，可以在重新生成时复用旧审核结果：

```bash
python3 -m bill2bean.cli review -c config.toml -o review.csv \
  --previous-review review.csv \
  '支付宝交易明细(20260601-20260618).csv' \
  '微信支付账单流水文件20260601_20260618_20260618172913.xlsx'
```

`--previous-review` 会按 `uid` 匹配旧核对表中的交易。匹配成功时，旧 CSV 中已编辑的 action、账户、AA/share、备注、tags/links 等核对字段会覆盖本次自动生成结果；未匹配的新交易仍按当前规则生成。这样可以在信用卡出账日前用微信/支付宝账单增量更新，出账后再加入信用卡账单定稿。

编辑 `review.csv` 后导出：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean
```

如果有账户定义文件：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean --accounts accounts.bean
```

如果希望 `imported.bean` 可以被 Fava 直接打开用于核对，可加文件头：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean --with-header --operating-currency CNY
```

这会在文件开头写入：

```beancount
include "accounts.bean"
option "operating_currency" "CNY"
option "render_commas" "True"
```

导出时也可以按来源和日期过滤：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean \
  --include-source wechat,alipay \
  --exclude-source icbc_credit \
  --start-date 2026-06-01 --end-date 2026-06-18
```

`--include-source` 和 `--exclude-source` 都可以重复使用，也可以用逗号分隔。过滤顺序是先 include，再 exclude；日期起止都包含，格式必须是 `YYYY-MM-DD`。如果用 `--accounts`，只会校验实际导出的分录账户。

支付宝基金/黄金交易默认不会随普通消费导出；它们在 `review.csv` 中使用 `action=invest`。需要导出时同时指定 commodity 文件、基金交易输出文件和价格文件：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean --with-header \
  --fund-commodities commodities.bean \
  --fund-output \
  --price-output
```

`--fund-output`、`--price-output` 可以显式给出文件名；如果选项后不跟参数，则表示使用 `-o` 的主输出文件。程序会先合并内容再写入，每个目标文件只写一次。默认只读取 `--price-output` 中已有的 `price` 指令；加 `--fetch-fund-prices` 才会调用 `bean-price -i -c` 抓取所需日期价格。启用基金导出且使用 `--with-header` 时，文件头还会 include `--fund-commodities`，并写入 `option "booking_method" "FIFO"` 和 `option "infer_tolerance_from_cost" "TRUE"`。价格缺失时，基金交易会用 `!` 暂记为 CNY 金额。

## 核对 CSV 约定

`review.csv` 的列顺序按人工核对流程排列：固定长度的 `uid` 放在行首，`action` 和 `review_level` 之后紧跟初始常为空、需要人工填写的 `aa_amount`、`share`、`share_amount`、`discount_amount` 以及投资覆盖字段；`direction`、`amount`、`currency` 紧跟 `source`，`review_reason` 放在金额信息之后，较长的 `source_id` 放在末尾。时间只输出 `time`，其中包含完整日期和时间；导出 Beancount 时会从 `time` 中取日期。

工行信用卡外币账单以“记账金额/币种”为 `amount` 和 `currency`，日期使用记账日。如果“交易金额/币种”和“记账金额/币种”不同，会额外填入 `original_amount` 和 `original_currency`；导出 Beancount 时消费分录会使用 `@@`，例如 `13100.00 CLP @@ 14.21 USD`。

输出行按分段排序：最前面优先输出 `post,manual` 人工审核行；如果这些行有 duplicate 子行，duplicate 会紧跟父行，方便对照。然后输出不含 `skip,ok` 且不含 `skip,check` duplicate 的主审核区，并同样把 `review_reason` 包含 `duplicate` 且指向另一行 `uid` 的 `skip,check` 行插到父行后面；最后把 `skip,ok` 行按时间顺序放到表尾。

`tags` 会作为 Beancount tag 导出到交易头，`links` 会作为 Beancount link 导出到交易头；多个值可以用空格、逗号或分号分隔。

`action` 可取：

- `post`：正常导出。
- `skip`：不导出，常用于重复项或中性交易。
- `receivable`：公务出差等报销条目。整笔实付净额进入 `receivable_account`，默认 `Assets:Receivables:Employer`。
- `transfer`：账户间转账。支付平台的信用卡还款会先作为候选转账，匹配到信用卡账单还款入账后再补全目标信用卡账户。
- `invest`：支付宝基金/黄金买入卖出。普通导出跳过，只有指定基金导出参数时才输出。
- `merge_cashback`：工商银行刷卡金自动行，不单独导出，会合并进上一条信用卡消费。按日期或来源过滤导出时，只要父消费被导出，对应刷卡金仍会合并进去。

基金 commodity 文件使用 `name` 元数据和支付宝基金名称精确匹配，`asset-class` 决定资产账户映射，`price` 元数据供 `bean-price` 使用；`settlement-days` 可以覆盖卖出确认价格日的 T+N 规则：

```beancount
2020-01-01 commodity SAMPLE_FUND_000001
  name: "示例基金C"
  asset-class: "fund"
  price: "CNY:eastmoneyfund/000001"
  settlement-days: "2"
```

配置中的基金账户按 `asset-class` 映射；如果 commodity 没有 `settlement-days`，卖出确认价格日的 T+N 规则按 pattern 顺序匹配：

```toml
[funds]
default_account = "Assets:Invest:Alipay:Fund"
default_income_account = "Income:Invest:Alipay:Fund"
commission_account = "Expenses:Invest:Commissions"
default_settlement_days = 1
share_precision = 2

[funds.accounts]
fund = "Assets:Invest:Alipay:Fund"
au9999 = "Assets:Invest:Alipay:AU9999"

[[funds.settlement_rules]]
pattern = "QDII|纳斯达克"
settlement_days = 2
```

`investment_units`、`investment_price`、`investment_price_date` 可以在 CSV 中人工覆盖自动计算；`commission_amount` 非空时会额外写入手续费账户，默认 `commission_account`。基金买卖都会处理 `discount_amount` 和 `commission_amount`：普通数字优惠视为抵扣，参与手续费净额 `net_fee = commission_amount - discount_amount`，买入按 `amount - net_fee` 确认份额，卖出按 `amount + net_fee` 确认份额；`discount_amount = cb:0.10` 或 `cashback:0.10` 视为另行返现，不改变确认金额，会额外写入资金账户入账和优惠收入。`cb:` 语法只支持投资交易，普通消费优惠仍使用纯数字。

信用卡返现商户可以在配置中添加规则。规则只匹配商户名，可选限制卡号；不要用“境外退货”等交易类型识别返现，因为真实退货也可能使用同一交易类型。配置规则识别出的返现会作为独立收入导出到 `cashback_income_account`，适合外币卡返现和原消费日期相隔较远的情况；工行人民币“刷卡金”仍使用 `merge_cashback` 合并到对应消费。

```toml
[[credit_card_cashback_rules]]
merchant_pattern = "Visa|Rewards|Rebate"
card_pattern = "1234"
```

`review_level` 为 `manual` 或 `check` 的行建议人工看一眼。`财付通(银联云闪付)` 会自动进入 `manual`，因为微信账单通常没有对应明细。

支付优惠通过 `discount_amount` 和 `discount_account` 处理。微信备注中的 `已优惠¥...` 会自动填入 `discount_amount`，支付宝 `收/付款方式` 含 `&` 时只能判断有优惠但没有金额，会标记为 `manual`，需要人工补金额。默认优惠收入账户是 `Income:Other`。

跨账单去重默认使用日期、金额、币种、出账账户和商户名文本匹配。`expense_rules` 只决定消费进入哪个 `Expenses:*` 账户，不参与去重。对于“信用卡账单显示公司主体名、微信/支付宝显示品牌或门店名”的情况，可以在配置中添加商户别名：

```toml
[[merchant_alias_rules]]
pattern = "公司主体名"
aliases = ["品牌名", "门店名"]
```

最终导出前会强制避免 `manual + skip`：如果一条交易仍是 `manual + skip`，会改成 `post` 并使用默认 `Expenses:Other` 或 `Income:Other` 兜底；如果未知的是出账账户或转账对方，则使用 `suspense_account`，默认 `Assets:Unknown`。这些兜底 posting 会导出为 Beancount 的 `!` posting flag，便于在 Beancount GUI 中作为 warning 筛查。普通默认支出分类或普通支付优惠进入 `Other` 不会自动加 `!`；只有 `review_reason` 中带 `flagged_account:<account>` 的账户会被标记。兜底账户仍需要在账户文件中 `open`。

共同支出可用 `share` 字段自动拆分，`aa_amount` 可用于偶发的对外 AA。`aa_amount` 会先扣除，`share` 再基于剩余实付净额计算共同付款对象的应收。

- `share=split`：按扣除 `aa_amount` 后的实付净额平分，一半进入 `share_account`。
- `share=whole`：扣除 `aa_amount` 后的剩余实付净额全部进入 `share_account`，适合亲属卡个人支出。
- `share=custom`：使用 `share_amount`。

微信亲属卡交易默认标记为 `share=whole`，默认账户为 `Assets:Receivables:Partner`。

`action=receivable` 用于公务报销，默认把整笔实付净额记入 `receivable_account`。如果同时填写 `aa_amount`，则 `aa_amount` 进入 `aa_account`，剩余净额进入 `receivable_account`，适合同一笔垫付中只有一部分由你本人申请报销、另一部分由同行人归还的情况。

`action=receivable` 与 `share` 不应共存。如果同时填写 `share` 或 `share_amount`，会标记 `manual` 并添加 `receivable_overrides_share`，导出时仍按公务报销处理。
