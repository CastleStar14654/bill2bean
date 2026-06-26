# bill2bean

把支付宝、微信支付和工商银行信用卡账单转换成可人工核对的 CSV，再导出 Beancount。

支持的输入文件：

- 支付宝导出的 CSV，或支付宝 App 邮件里的加密 ZIP。
- 微信支付导出的 xlsx。
- 工商银行信用卡对账单邮件 eml。

示例命令里的 `*` 表示平台导出文件名中的日期、时间戳等可变部分。

## Quick Start

生成核对表：

```bash
python3 -m bill2bean.cli review -c config.toml -o review.csv \
  '支付宝交易明细*.csv' \
  '微信支付账单流水文件*.xlsx' \
  '中国工商银行客户对账单*.eml'
```

支付宝 App 导出的加密 ZIP 可以直接传入。如果不使用 `--zip-password`，程序会在遇到加密 ZIP 时询问密码：

```bash
python3 -m bill2bean.cli review -c config.toml -o review.csv \
  '支付宝交易明细*.zip'
```

已有人工编辑过的核对表时，可以复用旧审核结果：

```bash
python3 -m bill2bean.cli review -c config.toml -o review.csv \
  --previous-review review.csv \
  '支付宝交易明细*.csv' \
  '微信支付账单流水文件*.xlsx'
```

`--previous-review` 按 `uid` 匹配旧行。旧 CSV 中已编辑的 action、账户、AA/share、备注、tags/links 等字段会覆盖本次自动生成结果；新交易仍按当前配置生成。这个流程适合在信用卡出账日前先用微信/支付宝账单增量核对，出账后加入信用卡账单定稿。

编辑 `review.csv` 后导出：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean
```

如果希望导出的文件能被 Fava 直接打开核对：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean \
  --with-header \
  --operating-currency CNY
```

这会写入 `include "accounts.bean"`、`option "operating_currency" "CNY"` 和 `option "render_commas" "True"`。

导出时可以筛选来源和日期：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean \
  --include-source wechat,alipay \
  --exclude-source icbc_credit \
  --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```

`--include-source` 和 `--exclude-source` 可以重复使用，也可以用逗号分隔。日期起止都包含。如果传入 `--accounts`，只校验实际导出分录用到的账户。

## Review CSV

`review.csv` 是主要人工界面。列顺序按核对流程排列：`uid` 在行首，`action` 和 `review_level` 后面是常需人工填写的 `aa_amount`、`share`、`share_amount`、`discount_amount` 和投资覆盖字段；长的 `source_id` 放在最后。日期不单独输出，导出 Beancount 时从 `time` 取日期。

常用 `action`：

- `post`：正常导出。
- `skip`：不导出，常用于重复项、平台内同账户转账和安全跳过的中性交易。
- `transfer`：账户间转账。支付平台发起的信用卡还款会先作为候选转账，匹配到信用卡账单还款入账后补全目标信用卡账户。
- `reimburse`：报销条目。金额进入 CSV 行的 `receivable_account`，其默认值来自配置项 `reimburse_account`。
- `invest`：支付宝基金/黄金买入卖出。普通导出跳过，只有使用基金导出参数时才输出。
- `merge_cashback`：工行刷卡金自动行，不单独导出，会合并到对应信用卡消费。

`review_level`：

- `ok`：通常无需处理。
- `check`：建议看一眼。
- `manual`：需要人工补全或确认。导出前若仍是 `manual + skip`，会强制改为 `post` 并使用默认账户或 `suspense_account` 兜底。

`tags` 会导出为 Beancount tag，`links` 会导出为 Beancount link；多个值可用空格、逗号或分号分隔。

输出行按审核优先级排序：`post,manual` 在最前；duplicate 子行紧跟父行；普通审核区随后；`skip,ok` 放在最后。

## Accounts And Rules

配置文件用几类规则决定账户：

- `account_rules`：把支付账户文本映射到 Assets/Liabilities。
- `expense_rules`：把消费文本映射到 Expenses。
- `income_rules`：把收入文本映射到 Income。
- `merchant_alias_rules`：辅助信用卡账单与微信/支付宝账单跨来源去重。

支付宝余额/余额宝、微信零钱/零钱通由 defaults 明确指定。代码会识别平台内部转账；如果你不想在账本里区分余额和理财账户，可以把对应配置指向同一个 Beancount 账户，同账户内部转账会自动 `skip,ok`。

未知出账账户或转账对方会使用 `suspense_account`，默认 `Assets:Unknown`。这些兜底 posting 会导出为 Beancount 的 `!` posting flag，方便在 Fava 中筛查。普通默认支出账户或普通优惠收入账户不会自动加 `!`；只有 `review_reason` 中带 `flagged_account:<account>` 的账户会被标记。

## Sharing And Reimbursement

共同支出用 `share` 字段处理，偶发对外 AA 用 `aa_amount`。计算顺序是先扣除 `aa_amount`，再基于剩余实付净额计算 `share`。

- `share=split`：剩余实付净额一半进入 `share_account`。
- `share=whole`：剩余实付净额全部进入 `share_account`，适合亲属卡个人支出。
- `share=custom`：使用 `share_amount`。

`share_account` 的默认值来自 `default_share_account`。微信亲属卡交易会自动标记 `share=whole`，但这个默认账户同样适用于手动填写的 share。

`action=reimburse` 用于公务报销等场景。默认整笔实付净额进入 `receivable_account`；如果同时填写 `aa_amount`，则 `aa_amount` 进入 `aa_account`，剩余净额进入 `receivable_account`。`reimburse` 与 `share` 不应共存；如果同时填写，会标记 `manual` 并添加 `reimburse_overrides_share`，导出仍按报销处理。

如果一笔支出在账务实质上完全不是你的消费，可以直接在 review 中把 `expense_account` 改成对应的 `Assets:Receivables:*` 账户，不必使用 `share`。

## Automatic Handling

跨账单去重默认使用日期、金额、币种、出账账户和商户文本匹配。`expense_rules` 只决定消费分类，不参与去重。

信用卡返现分为两类。实时返现会合并到对应主消费，例如工行人民币“刷卡金”；非实时返现和原消费往往不相邻，例如境外刷卡活动返现，会作为单独收入导出。默认情况下，两类返现都会进入 `cashback_income_account`。工行刷卡金可以用 `icbc_shuakajin_income_account` 单独指定收入账户。

非实时返现可用配置规则识别：

```toml
[[credit_card_cashback_rules]]
merchant_pattern = "Visa|Rewards|Rebate"
card_pattern = "1234"
income_account = "Income:Rebate:Visa"
```

规则匹配商户名，可选限制卡号，也可以用 `income_account` 指定这一类返现的收入账户；未指定时使用 `cashback_income_account`。配置规则识别出的返现会作为独立收入导出，不需要修改代码。新增会合并到主消费的实时返现模式目前需要修改 parser/匹配代码；已支持的工行实时刷卡金使用 `merge_cashback` 合并到对应消费。

支付优惠通过 `discount_amount` 和 `discount_account` 处理。`review.csv` 中的 `amount` 是账单给出的实付金额；如果存在支付优惠，`discount_amount` 表示在实付金额之外额外冲减的消费金额。微信备注中的 `已优惠...` 会自动填入 `discount_amount`。支付宝 `收/付款方式` 含 `&` 时只能判断有优惠但没有金额，会标记为 `manual`，需要人工补金额。默认优惠收入账户是 `discount_income_account`。

工行信用卡外币账单以“记账金额/币种”为 `amount` 和 `currency`，日期使用记账日。如果“交易金额/币种”和“记账金额/币种”不同，会额外填入 `original_amount` 和 `original_currency`；导出时消费分录使用 Beancount `@@`。

## Investment Export

支付宝基金/黄金交易在 review 中标记为 `action=invest`，默认不会随普通消费导出。需要导出投资交易时同时指定 commodity 文件、基金交易输出文件和价格文件：

```bash
python3 -m bill2bean.cli export review.csv -o imported.bean \
  --accounts accounts.bean \
  --with-header \
  --fund-commodities commodities.bean \
  --fund-output \
  --price-output
```

`--fund-output` 和 `--price-output` 可以显式给出文件名；如果选项后不跟参数，则使用 `-o` 的主输出文件。程序会先合并内容再写入，每个目标文件只写一次。

默认只读取 `--price-output` 中已有的 `price` 指令；加 `--fetch-fund-prices` 才会调用 `bean-price -i -c` 抓取所需日期价格。启用基金导出且使用 `--with-header` 时，文件头还会 include `--fund-commodities`，并写入：

```beancount
option "booking_method" "FIFO"
option "infer_tolerance_from_cost" "TRUE"
```

Commodity 文件用 `name` 元数据和支付宝基金名称精确匹配，`asset-class` 决定资产账户映射，`price` 元数据供 `bean-price` 使用；`settlement-days` 可以覆盖卖出确认价格日的 T+N 规则：

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

`investment_units`、`investment_price`、`investment_price_date` 可以在 CSV 中人工覆盖自动计算。`commission_amount` 非空时会额外写入手续费账户。基金买卖都会处理 `discount_amount` 和 `commission_amount`：普通数字优惠视为抵扣；`discount_amount = cb:0.10` 或 `cashback:0.10` 视为另行返现，不改变确认金额，会额外写入资金账户入账和优惠收入。`cb:` 语法只支持投资交易。
