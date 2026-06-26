from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable
import zipfile
from xml.etree import ElementTree as ET

from .model import BillTransaction, Direction, ReviewLevel, ReviewReasons

if TYPE_CHECKING:
    from .config import Config


class BillParser(ABC):
    """Convert a bill file to transactions plus normalizer-facing metadata facts."""

    source: str

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def parse(self, path: str | Path) -> list[BillTransaction]:
        raise NotImplementedError


def _money(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("¥", "").replace("￥", "").strip()
    return Decimal(cleaned).quantize(Decimal("0.01"))


class AlipayCsvParser(BillParser):
    """Parse Alipay rows and annotate source-specific facts for normalization."""

    source = "alipay"
    direction_map = {
        "支出": Direction.EXPENSE,
        "收入": Direction.INCOME,
        "不计收支": Direction.NEUTRAL,
    }

    def parse(self, path: str | Path) -> list[BillTransaction]:
        text = Path(path).read_text(encoding="gb18030")
        return self.parse_text(text)

    def parse_text(self, text: str) -> list[BillTransaction]:
        lines = text.splitlines()
        header_index = next(i for i, line in enumerate(lines) if line.startswith("交易时间,"))
        rows = csv.DictReader(lines[header_index:])
        txs: list[BillTransaction] = []
        for row in rows:
            if not row.get("交易时间"):
                continue
            txs.append(self._parse_row(row))
        return txs

    def _parse_row(self, row: dict[str, str]) -> BillTransaction:
        payee = (row.get("交易对方") or "").strip()
        narration = (row.get("商品说明") or "").strip()
        payment_method = (row.get("收/付款方式") or "").strip()
        direction = self._direction(row, payee, narration)
        source_account = self.config.alipay_account_for_method(payment_method)
        metadata = {k: (v or "").strip() for k, v in row.items() if k}
        if "&" in payment_method:
            metadata["discount_amount_unknown"] = "true"
        source_account = self._apply_account_metadata(
            row,
            payee,
            narration,
            payment_method,
            source_account,
            metadata,
        )
        self._apply_investment_metadata(row, payee, metadata)
        return BillTransaction(
            source=self.source,
            source_id=(row.get("交易订单号") or row.get("商家订单号") or "").strip(),
            time=datetime.strptime(row["交易时间"].strip(), "%Y-%m-%d %H:%M:%S"),
            payee=payee,
            narration=narration,
            amount=_money(row.get("金额", "0")),
            direction=direction,
            source_account_hint=source_account,
            metadata=metadata,
        )

    def _direction(self, row: dict[str, str], payee: str, narration: str) -> Direction:
        if self._is_yuebao_yield(payee, narration):
            return Direction.INCOME
        return self.direction_map.get(row.get("收/支", "").strip(), Direction.NEUTRAL)

    def _apply_account_metadata(
        self,
        row: dict[str, str],
        payee: str,
        narration: str,
        payment_method: str,
        source_account: str,
        metadata: dict[str, str],
    ) -> str:
        if self._is_yuebao_yield(payee, narration):
            metadata["platform_income"] = "alipay_yuebao_yield"
            return self.config.alipay_yuebao_account
        if self._is_yuebao_transfer(payee, narration):
            source_account, target_account = self._alipay_yuebao_transfer_accounts(
                narration,
                payment_method,
            )
            metadata["target_account_hint"] = target_account
            metadata["platform_transfer"] = "alipay_yuebao"
            return source_account
        if self._is_huabei_repayment(row):
            metadata["target_account_hint"] = self.config.alipay_huabei_account
            metadata["platform_transfer"] = "alipay_huabei_repayment"
        return source_account

    def _apply_investment_metadata(
        self,
        row: dict[str, str],
        payee: str,
        metadata: dict[str, str],
    ) -> None:
        if self._is_investment_buy_refund(row):
            metadata["investment_buy_refund"] = "true"
        elif self._is_investment_trade(row):
            metadata["investment_trade"] = "true"
        elif self._is_safe_neutral_investment(row, payee):
            metadata["safe_neutral_skip"] = "true"

    def _alipay_yuebao_transfer_accounts(
        self,
        narration: str,
        payment_method: str,
    ) -> tuple[str, str]:
        method_account = self.config.alipay_account_for_method(payment_method)
        if "转出" in narration:
            return self.config.alipay_yuebao_account, method_account
        return method_account, self.config.alipay_yuebao_account

    @classmethod
    def _is_yuebao_transfer(cls, payee: str, narration: str) -> bool:
        return (
            payee == "余额宝" or narration.startswith("余额宝-")
        ) and "收益发放" not in narration

    @classmethod
    def _is_yuebao_yield(cls, payee: str, narration: str) -> bool:
        return payee == "余额宝" and "收益发放" in narration

    @classmethod
    def _is_huabei_repayment(cls, row: dict[str, str]) -> bool:
        if (row.get("交易分类") or "").strip() != "信用借还":
            return False
        text = " ".join(
            [
                row.get("交易对方") or "",
                row.get("商品说明") or "",
            ]
        )
        return "花呗" in text

    @classmethod
    def _is_investment_buy_refund(cls, row: dict[str, str]) -> bool:
        narration = row.get("商品说明") or ""
        status = row.get("交易状态") or ""
        return "买入退款" in narration and "退款成功" in status

    @classmethod
    def _is_investment_trade(cls, row: dict[str, str]) -> bool:
        payee = row.get("交易对方") or ""
        narration = row.get("商品说明") or ""
        status = row.get("交易状态") or ""
        if (row.get("交易分类") or "").strip() != "投资理财":
            return False
        if payee == "余额宝" or "余额宝-收益发放" in narration:
            return False
        if "退款" in narration or "退款" in status:
            return False
        if "蚂蚁（杭州）基金销售有限公司" not in payee:
            return False
        return "买入" in narration or "卖出" in narration

    @classmethod
    def _is_safe_neutral_investment(cls, row: dict[str, str], payee: str) -> bool:
        if payee == "余额宝":
            return False
        text = " ".join(
            [
                row.get("交易对方") or "",
                row.get("商品说明") or "",
                str(row),
            ]
        )
        return (
            (row.get("交易分类") or "").strip() == "投资理财"
            or "蚂蚁财富" in text
            or "基金" in text
            or "黄金ETF" in text
        )


class AlipayZipParser(BillParser):
    source = "alipay"

    def __init__(
        self,
        config: Config,
        password_provider: Callable[[str | Path], str] | None = None,
    ) -> None:
        super().__init__(config)
        self.password_provider = password_provider

    def parse(self, path: str | Path) -> list[BillTransaction]:
        path = Path(path)
        with zipfile.ZipFile(path) as zf:
            csv_infos = [
                info
                for info in zf.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".csv")
            ]
            if len(csv_infos) != 1:
                raise ValueError(
                    f"Alipay zip must contain exactly one CSV file: {path}"
                )
            info = csv_infos[0]
            password = self._password(path) if _zip_info_is_encrypted(info) else ""
            try:
                with zf.open(info, pwd=password.encode() if password else None) as fh:
                    text = fh.read().decode("gb18030")
            except RuntimeError as exc:
                raise ValueError(f"failed to decrypt Alipay zip: {path}") from exc
        return AlipayCsvParser(self.config).parse_text(text)

    def _password(self, path: Path) -> str:
        if not self.password_provider:
            raise ValueError(f"Alipay zip is encrypted and requires a password: {path}")
        password = self.password_provider(path)
        if not password:
            raise ValueError(f"Alipay zip password is empty: {path}")
        return password


class WechatXlsxParser(BillParser):
    """Parse WeChat XLSX rows and expose platform transfers as generic metadata."""

    source = "wechat"
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    direction_map = {
        "支出": Direction.EXPENSE,
        "收入": Direction.INCOME,
        "中性交易": Direction.NEUTRAL,
        "/": Direction.NEUTRAL,
    }

    def parse(self, path: str | Path) -> list[BillTransaction]:
        rows = self._read_rows(path)
        header_index = next(i for i, row in enumerate(rows) if row and row[0] == "交易时间")
        headers = rows[header_index]
        txs: list[BillTransaction] = []
        for values in rows[header_index + 1 :]:
            if not values or not values[0]:
                continue
            row = dict(zip(headers, values))
            txs.append(self._parse_row(row))
        return txs

    def _parse_row(self, row: dict[str, str]) -> BillTransaction:
        payment_method = (row.get("支付方式") or "").strip()
        status = (row.get("当前状态") or "").strip()
        source_account = self.config.wechat_account_for_method(payment_method, status)
        metadata = dict(row)
        narration = (row.get("商品") or "").strip()
        if self._is_lqt_transfer(row):
            source_account, target_account = self._wechat_lqt_transfer_accounts(row)
            metadata["target_account_hint"] = target_account
            metadata["platform_transfer"] = "wechat_lqt"
            if not narration or narration == "/":
                narration = (row.get("交易类型") or "").strip()
        tx = BillTransaction(
            source=self.source,
            source_id=(row.get("交易单号") or row.get("商户单号") or "").strip(),
            time=self._excel_time(row["交易时间"]),
            payee=(row.get("交易对方") or "").strip(),
            narration=narration,
            amount=_money(row.get("金额(元)", "0")),
            direction=self._direction(row),
            source_account_hint=source_account,
            metadata=metadata,
        )
        discount = self._discount(row.get("备注", ""))
        if discount:
            tx.metadata["discount_amount"] = str(discount)
        return tx

    def _direction(self, row: dict[str, str]) -> Direction:
        return self.direction_map.get(row.get("收/支", "").strip(), Direction.NEUTRAL)

    def _wechat_lqt_transfer_accounts(self, row: dict[str, str]) -> tuple[str, str]:
        txn_type = row.get("交易类型", "")
        if "转入零钱通" in txn_type:
            source_text = txn_type.split("来自", 1)[1] if "来自" in txn_type else ""
            if source_text == "零钱":
                source_account = self.config.wechat_balance_account
            else:
                source_account = self.config.account_for_text(source_text) or source_text
            return source_account, self.config.wechat_lqt_account
        target_text = txn_type.split("到", 1)[1] if "到" in txn_type else ""
        target_account = self.config.account_for_text(target_text) or target_text
        return self.config.wechat_lqt_account, target_account

    def _read_rows(self, path: str | Path) -> list[list[str]]:
        with zipfile.ZipFile(path) as zf:
            shared = self._shared_strings(zf)
            sheet_name = next(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
            root = ET.fromstring(zf.read(sheet_name))
            rows: list[list[str]] = []
            for row_node in root.findall(".//a:row", self.ns):
                values = []
                for cell in row_node.findall("a:c", self.ns):
                    ref = cell.attrib.get("r", "")
                    if ref:
                        column = _xlsx_column_index(ref)
                        while len(values) < column:
                            values.append("")
                    value_node = cell.find("a:v", self.ns)
                    raw = value_node.text if value_node is not None else ""
                    if cell.attrib.get("t") == "s" and raw:
                        values.append(shared[int(raw)])
                    elif cell.attrib.get("t") == "inlineStr":
                        values.append(
                            "".join(t.text or "" for t in cell.findall(".//a:t", self.ns))
                        )
                    else:
                        values.append(raw or "")
                rows.append(values)
            return rows

    def _shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        return [
            "".join(t.text or "" for t in si.findall(".//a:t", self.ns))
            for si in root.findall("a:si", self.ns)
        ]

    def _excel_time(self, value: str) -> datetime:
        if re.match(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value)
        serial = Decimal(value)
        return datetime(1899, 12, 30) + timedelta(days=float(serial))

    @classmethod
    def _discount(cls, note: str) -> Decimal:
        match = re.search(r"已优惠[¥￥]?([\d.]+)", note or "")
        if not match:
            return Decimal("0.00")
        return _money(match.group(1))

    @classmethod
    def _is_lqt_transfer(cls, row: dict[str, str]) -> bool:
        txn_type = row.get("交易类型", "")
        return "转入零钱通" in txn_type or "零钱通转出" in txn_type


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class IcbcEmailParser(BillParser):
    """Parse ICBC credit-card statement emails into posted card transactions."""

    source = "icbc_credit"
    detail_table_markers = (
        "人民币(本位币) 交 易 明 细",
        "外 币 交 易 明 细",
    )
    flow_direction_map = {
        "支出": Direction.EXPENSE,
        "存入": Direction.INCOME,
    }

    def parse(self, path: str | Path) -> list[BillTransaction]:
        msg = BytesParser(policy=policy.default).parsebytes(Path(path).read_bytes())
        html = next(
            part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            for part in msg.walk()
            if part.get_content_type() == "text/html"
        )
        parser = _TableParser()
        parser.feed(html)
        txs: list[BillTransaction] = []
        previous_postable: BillTransaction | None = None
        detail_rows = self._detail_rows(parser.rows)
        rmb_card = self._rmb_unionpay_card(detail_rows)
        for row_number, row in detail_rows:
            tx = self._parse_detail_row(row, row_number, rmb_card)
            if self._is_shuakajin(tx) and previous_postable:
                tx.action = "merge_cashback"
                tx.income_account = self.config.icbc_shuakajin_income_account
                if "退款" in tx.payee:
                    tx.amount = -tx.amount
                    tx.direction = Direction.INCOME
                tx.review_reason = ReviewReasons.parse(f"cashback_for:{previous_postable.uid}")
                previous_postable.metadata.setdefault("cashbacks", []).append(
                    {"amount": str(tx.amount), "payee": tx.payee}
                )
            else:
                previous_postable = tx
            txs.append(tx)
        return txs

    def _detail_rows(self, rows: list[list[str]]) -> list[tuple[int, list[str]]]:
        in_details = False
        detail_rows: list[tuple[int, list[str]]] = []
        for row_number, row in enumerate(rows, start=1):
            row_text = " ".join(row)
            if any(marker in row_text for marker in self.detail_table_markers):
                in_details = True
                continue
            if in_details and len(row) >= 7 and re.fullmatch(r"\d{4}", row[0]):
                detail_rows.append((row_number, row[:7]))
        return detail_rows

    def _is_shuakajin(self, tx: BillTransaction) -> bool:
        return tx.metadata["txn_type"] == "刷卡金" and (
            "入账" in tx.payee or "退款" in tx.payee
        )

    def _parse_detail_row(
        self, row: list[str], row_number: int, rmb_card: str | None
    ) -> BillTransaction:
        card, txn_date, post_date, txn_type, merchant, txn_amount, post_amount = row
        txn_amount_text, txn_currency = self._parse_txn_amount(txn_amount)
        amount_text, currency, flow = self._parse_post_amount(post_amount)
        normalized_txn_amount = str(_money(txn_amount_text))
        normalized_post_amount = str(_money(amount_text))
        account_card = card
        metadata = {
            "card": card,
            "txn_date": txn_date,
            "post_date": post_date,
            "txn_type": txn_type,
            "txn_amount": txn_amount,
            "txn_amount_value": normalized_txn_amount,
            "txn_currency": txn_currency,
            "post_amount": post_amount,
            "post_amount_value": normalized_post_amount,
            "post_currency": currency,
        }
        if txn_currency != currency:
            metadata["original_amount"] = normalized_txn_amount
            metadata["original_currency"] = txn_currency
        if self._is_repayment_like(txn_type, merchant, flow):
            metadata["is_credit_card_repayment"] = "true"
            if currency == "CNY" and rmb_card and card != rmb_card:
                account_card = rmb_card
                metadata["account_card"] = account_card
                metadata["original_card"] = card
            if txn_type != "信用卡还款":
                metadata["original_txn_type"] = txn_type
                metadata["txn_type"] = "信用卡还款"
        direction = self.flow_direction_map.get(flow, Direction.INCOME)
        review_level = ReviewLevel.OK
        review_reason = ""
        if "财付通(银联云闪付)" in merchant:
            review_level = ReviewLevel.MANUAL
            review_reason = "unionpay_via_tenpay_missing_from_wechat"
        tx = BillTransaction(
            source=self.source,
            source_id=f"row:{row_number}|" + "|".join(row),
            time=datetime.strptime(post_date, "%Y-%m-%d"),
            payee=merchant,
            narration=txn_type,
            amount=_money(amount_text),
            currency=currency,
            direction=direction,
            source_account_hint=f"ICBC信用卡({account_card})",
            review_level=review_level,
            review_reason=review_reason,
            metadata=metadata,
        )
        if tx.direction == Direction.INCOME:
            cashback_income_account = self.config.credit_card_cashback_income_account(tx)
        else:
            cashback_income_account = ""
        if cashback_income_account:
            tx.metadata["is_credit_card_cashback"] = "true"
            tx.income_account = cashback_income_account
            tx.review_reason.add("credit_card_cashback")
        return tx

    def _parse_txn_amount(self, value: str) -> tuple[str, str]:
        match = re.match(r"([\d,.]+)/([A-Z]+)$", value)
        if not match:
            raise ValueError(f"unsupported ICBC transaction amount: {value}")
        return match.group(1), self._beancount_currency(match.group(2))

    def _parse_post_amount(self, value: str) -> tuple[str, str, str]:
        match = re.match(r"([\d,.]+)/([A-Z]+)\(([^)]+)\)", value)
        if not match:
            raise ValueError(f"unsupported ICBC amount: {value}")
        return match.group(1), self._beancount_currency(match.group(2)), match.group(3)

    def _beancount_currency(self, currency: str) -> str:
        return "CNY" if currency == "RMB" else currency

    def _rmb_unionpay_card(self, rows: list[tuple[int, list[str]]]) -> str | None:
        cards: Counter[str] = Counter()
        for _row_number, row in rows:
            card, _txn_date, _post_date, txn_type, merchant, _txn_amount, post_amount = row
            try:
                _amount_text, currency, flow = self._parse_post_amount(post_amount)
            except ValueError:
                continue
            if (
                currency == "CNY"
                and flow == "支出"
                and txn_type not in {"刷卡金"}
                and "退款" not in txn_type
            ):
                cards[card] += 1
        if not cards:
            return None
        return cards.most_common(1)[0][0]

    def _is_repayment_like(self, txn_type: str, merchant: str, flow: str) -> bool:
        if flow != "存入" or "退款" in merchant:
            return False
        if txn_type in {"信用卡还款", "转账", "转帐"}:
            return True
        if txn_type == "他行汇入" and "牡丹卡中心" in merchant:
            return True
        return txn_type == "银联入账" and "银联转账" in merchant


def parser_for(
    path: str | Path,
    config: Config,
    zip_password_provider: Callable[[str | Path], str] | None = None,
) -> BillParser:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if "支付宝" in name and suffix == ".csv":
        return AlipayCsvParser(config)
    if "支付宝" in name and suffix == ".zip":
        return AlipayZipParser(config, zip_password_provider)
    if "微信" in name and suffix == ".xlsx":
        return WechatXlsxParser(config)
    if "工商银行" in name and suffix == ".eml":
        return IcbcEmailParser(config)
    raise ValueError(f"cannot infer parser for {path}")


def _zip_info_is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def _xlsx_column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1
