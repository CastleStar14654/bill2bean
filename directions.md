下面按当前代码里的导出逻辑画。记号：

- `A = amount`
- `D = discount_amount`
- `C = commission_amount`
- `CB = cashback`
- `AA = aa_amount`
- `S = share_amount`，或由 `share=split/whole` 自动算出

## 支出
### 普通支出
`action=post, direction=expense`

有 AA/share：

```text
source_account  --A - CB-->  expense_account + aa_account + share_account
discount_account --D + CB--> expense_account  # 若有 D，表现为优惠收入冲减支出
```

```bean
expense_account    A + D - AA - S
aa_account         AA
share_account      S
discount_account  -D 
discount_account  -CB
source_account    -A + CB
```

`commission_amount` 不支持普通 expense 行。

### 报销
`action=reimburse, direction=expense`

同 普通支出，但是 expense_account 变为 receivable_account。
`reimburse` 不支持 `share`。

## 收入
`action=post, direction=income`

```text
income_account  --A-->  source_account
```

```bean
source_account   A
income_account  -A
```

收入行不支持 AA/share/discount/commission。

## 本人转帐
### 普通转账
`action=transfer, direction=transfer`

这里 `expense_account` 实际是“转入账户”，不是支出账户。

```text
source_account  --A-------->  expense_account
discount_account --D-------->  expense_account
commission_account <--C------  expense_account
```

```bean
expense_account     A + D - C
discount_account   -D
commission_account  C
source_account     -A
```

### 收入方向转账
`action=transfer, direction=income`

这是为了方便把原始 income 行改成 transfer。此时：

- `source_account` 是入账账户
- `income_account` 是出账账户

```text
income_account  --(A - D + C)-->  source_account
discount_account --D------------>  source_account
commission_account <--C----------  source_account
```

```bean
source_account      A
discount_account   -D
commission_account  C
income_account     -(A - D + C)
```

## 外币/带 @@ 的交易
这里：

- `A = amount`，信用卡记账金额
- `CUR = currency`，信用卡记账币种
- `OA = original_amount`，原始消费金额
- `OCUR = original_currency`，原始消费币种

### 消费
`action=post, direction=expense`，且有 `original_amount/original_currency`

```text
source_account  --A CUR-->  expense_account
expense_account 以 OA OCUR @@ A CUR 记录原始消费币种
```

```bean
expense_account   OA OCUR @@ A CUR
source_account   -A CUR
```

## 转帐
`action=transfer` 且有 `original_amount`，不支持 `discount_amount` / `commission_amount`。

### `direction=transfer`

```text
source_account  --OA OCUR-->
expense_account <--A CUR--
```

```bean
expense_account  A CUR @@ OA OCUR
source_account  -OA OCUR
```

### `direction=income`

```text
income_account  --OA OCUR-->
source_account <--A CUR--
```

```bean
source_account  A CUR @@ OA OCUR
income_account -OA OCUR
```

## CSV 默认值提示
当前 review 里：

- 所有 `direction=expense` 行会填 `receivable_account`、`aa_account`、`share_account`，方便人工改成报销/AA/share。
- `discount_account`、`commission_account` 可以为空；导出时如果对应金额非空，会用配置默认值。
- 非 expense 行不会为了提示而填 receivable/aa/share。
