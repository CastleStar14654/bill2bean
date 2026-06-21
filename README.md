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

## 核对 CSV 约定

`review.csv` 的列顺序按人工核对流程排列：固定长度的 `uid` 放在行首，`action` 和 `review_level` 之后紧跟初始常为空、需要人工填写的 `aa_amount`、`share`、`share_amount`、`discount_amount`；`direction`、`amount`、`currency` 紧跟 `source`，`review_reason` 放在 `currency` 之后，较长的 `source_id` 放在末尾。时间只输出 `time`，其中包含完整日期和时间；导出 Beancount 时会从 `time` 中取日期。

输出行按分段排序：最前面优先输出 `post,manual` 人工审核行；如果这些行有 duplicate 子行，duplicate 会紧跟父行，方便对照。然后输出不含 `skip,ok` 且不含 `skip,check` duplicate 的主审核区，并同样把 `review_reason` 包含 `duplicate` 且指向另一行 `uid` 的 `skip,check` 行插到父行后面；最后把 `skip,ok` 行按时间顺序放到表尾。

`tags` 会作为 Beancount tag 导出到交易头，`links` 会作为 Beancount link 导出到交易头；多个值可以用空格、逗号或分号分隔。

`action` 可取：

- `post`：正常导出。
- `skip`：不导出，常用于重复项或中性交易。
- `receivable`：公务出差等报销条目。整笔实付净额进入 `receivable_account`，默认 `Assets:Receivables:Employer`。
- `transfer`：账户间转账。支付平台的信用卡还款会先作为候选转账，匹配到信用卡账单还款入账后再补全目标信用卡账户。
- `merge_cashback`：工商银行刷卡金自动行，不单独导出，会合并进上一条信用卡消费。按日期或来源过滤导出时，只要父消费被导出，对应刷卡金仍会合并进去。

`review_level` 为 `manual` 或 `check` 的行建议人工看一眼。`财付通(银联云闪付)` 会自动进入 `manual`，因为微信账单通常没有对应明细。

支付优惠通过 `discount_amount` 和 `discount_account` 处理。微信备注中的 `已优惠¥...` 会自动填入 `discount_amount`，支付宝 `收/付款方式` 含 `&` 时只能判断有优惠但没有金额，会标记为 `manual`，需要人工补金额。默认优惠收入账户是 `Income:Other`。

最终导出前会强制避免 `manual + skip`：如果一条交易仍是 `manual + skip`，会改成 `post` 并使用 `manual_expense_account` 或 `manual_income_account` 兜底，便于在 Beancount GUI 中筛查。

共同支出可用 `share` 字段自动拆分，`aa_amount` 可用于偶发的对外 AA。`aa_amount` 会先扣除，`share` 再基于剩余实付净额计算共同付款对象的应收。

- `share=split`：按扣除 `aa_amount` 后的实付净额平分，一半进入 `share_account`。
- `share=whole`：扣除 `aa_amount` 后的剩余实付净额全部进入 `share_account`，适合亲属卡个人支出。
- `share=custom`：使用 `share_amount`。

微信亲属卡交易默认标记为 `share=whole`，默认账户为 `Assets:Receivables:Partner`。

`action=receivable` 用于公务报销，默认把整笔实付净额记入 `receivable_account`。如果同时填写 `aa_amount`，则 `aa_amount` 进入 `aa_account`，剩余净额进入 `receivable_account`，适合同一笔垫付中只有一部分由你本人申请报销、另一部分由同行人归还的情况。

`action=receivable` 与 `share` 不应共存。如果同时填写 `share` 或 `share_amount`，会标记 `manual` 并添加 `receivable_overrides_share`，导出时仍按公务报销处理。
