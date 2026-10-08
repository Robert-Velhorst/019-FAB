import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from src.operations.local_ledger import LocalOperationsLedger
from src.validation.financial_consistency import finite_number


FINAL_RECONCILIATION_STATUSES = {"approved", "reconciled", "ignored"}
OPEN_RECONCILIATION_STATUSES = ("not_started", "candidate", "missing_receipt", "needs_review", "rejected", "resolved")
MAX_BANK_STATEMENT_BYTES = 4 * 1024 * 1024
SUPPORTED_BANK_STATEMENT_FORMATS = {"csv", "json", "camt", "mt940"}
BANK_STATEMENT_FILE_EXTENSIONS = {
    "csv": {".csv"},
    "json": {".json"},
    "camt": {".camt", ".xml"},
    "mt940": {".mt940", ".sta"},
}
CSV_DATE_FIELDS = {
    "transactiondate",
    "date",
    "datum",
    "bookingdate",
    "boekingsdatum",
    "booked",
    "valuedate",
    "valutadatum",
}
CSV_AMOUNT_FIELDS = {
    "amount",
    "transactionamount",
    "bedrag",
    "value",
    "debit",
    "af",
    "withdrawal",
    "credit",
    "bij",
    "deposit",
}


class LocalBankTransactionImportService:
    """Normalize bank/account transaction exports into FAB's local ledger."""

    def __init__(self, ledger: LocalOperationsLedger, config: Optional[Dict[str, Any]] = None):
        self.ledger = ledger
        self.config = config or {}

    def import_statement_text(
        self,
        statement_text: str,
        format: str = "csv",
        account_identifier: str = "default",
        source: str = "manual_upload",
        filename: Optional[str] = None,
        actor: str = "local_bank_import",
    ) -> Dict[str, Any]:
        format_name = str(format or "csv").strip().lower()
        text = statement_text or ""
        if not text.strip():
            raise ValueError("statementText is required")
        if len(text.encode("utf-8")) > MAX_BANK_STATEMENT_BYTES:
            raise ValueError(
                f"Bank statement exceeds the {MAX_BANK_STATEMENT_BYTES}-byte import limit"
            )
        if format_name == "json":
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                parsed = parsed.get("bankTransactions") or parsed.get("transactions") or []
            if not isinstance(parsed, list):
                raise ValueError("JSON statement text must be a list or contain transactions")
            return self.import_transactions(
                parsed,
                account_identifier,
                source,
                filename,
                "json",
                actor=actor,
            )
        if format_name == "csv":
            return self.import_transactions(
                _parse_csv_transactions(text),
                account_identifier,
                source,
                filename,
                "csv",
                actor=actor,
            )
        if format_name in {"camt", "xml", "camt.053", "camt053"}:
            return self.import_transactions(
                _parse_camt_transactions(text),
                account_identifier,
                source,
                filename,
                "camt",
                actor=actor,
            )
        if format_name in {"mt940", "sta"}:
            return self.import_transactions(
                _parse_mt940_transactions(text),
                account_identifier,
                source,
                filename,
                "mt940",
                actor=actor,
            )
        raise ValueError(f"Unsupported bank statement format: {format}")

    def import_statement_bytes(
        self,
        statement_bytes: bytes,
        format: str = "csv",
        account_identifier: str = "default",
        source: str = "manual_upload",
        filename: Optional[str] = None,
        actor: str = "local_bank_import",
    ) -> Dict[str, Any]:
        if not isinstance(statement_bytes, bytes) or not statement_bytes:
            raise ValueError("Bank statement file is empty")
        if len(statement_bytes) > MAX_BANK_STATEMENT_BYTES:
            raise ValueError(
                f"Bank statement exceeds the {MAX_BANK_STATEMENT_BYTES}-byte import limit"
            )
        return self.import_statement_text(
            _decode_statement_bytes(statement_bytes),
            format=format,
            account_identifier=account_identifier,
            source=source,
            filename=filename,
            actor=actor,
        )

    def import_transactions(
        self,
        transactions: Iterable[Dict[str, Any]],
        account_identifier: str = "default",
        source: str = "manual_json",
        filename: Optional[str] = None,
        format: str = "json",
        actor: str = "local_bank_import",
    ) -> Dict[str, Any]:
        if not isinstance(transactions, list):
            transactions = list(transactions or [])
        account_identifier = str(account_identifier or "default").strip() or "default"
        account_identifier = account_identifier[:200]
        source = str(source or "manual_json").strip()[:120] or "manual_json"
        filename = str(filename or "").strip()[:255] or None
        actor = str(actor or "local_bank_import").strip()[:200] or "local_bank_import"
        with self.ledger.write_transaction():
            return self._record_transaction_import(
                transactions,
                account_identifier=account_identifier,
                source=source,
                filename=filename,
                format=format,
                actor=actor,
            )

    def _record_transaction_import(
        self,
        transactions: List[Dict[str, Any]],
        account_identifier: str,
        source: str,
        filename: Optional[str],
        format: str,
        actor: str,
    ) -> Dict[str, Any]:
        import_id = self.ledger.create_bank_statement_import({
            "source": source,
            "accountIdentifier": account_identifier,
            "filename": filename,
            "format": format,
            "status": "running",
            "rowsSeen": len(transactions),
        })
        imported = 0
        duplicates = 0
        skipped: List[Dict[str, Any]] = []
        bank_transaction_ids: List[int] = []
        generated_occurrences: Dict[str, int] = {}

        for index, transaction in enumerate(transactions):
            if not isinstance(transaction, dict):
                skipped.append({"row": index, "reason": "not_an_object"})
                continue
            normalized = normalize_bank_transaction(transaction, account_identifier, source)
            if not _has_financial_signal(normalized):
                skipped.append({"row": index, "reason": "missing_required_financial_fields"})
                continue
            if normalized["metadata"]["generatedTransactionId"]:
                fingerprint = str(normalized["duplicateFingerprint"])
                occurrence = generated_occurrences.get(fingerprint, 0) + 1
                generated_occurrences[fingerprint] = occurrence
                normalized["metadata"]["generatedOccurrence"] = occurrence
                if occurrence > 1:
                    normalized["transactionId"] = (
                        f"{normalized['transactionId']}:{occurrence}"
                    )
            existing = self.ledger.get_bank_transaction_by_identity(
                normalized["accountIdentifier"],
                normalized["transactionId"],
            )
            transaction_record_id = self.ledger.upsert_bank_transaction({
                **normalized,
                "importId": import_id,
            })
            bank_transaction_ids.append(transaction_record_id)
            if existing:
                duplicates += 1
            else:
                imported += 1

        status = "completed" if imported or duplicates else "empty"
        self.ledger.update_bank_statement_import(import_id, {
            "status": status,
            "rowsSeen": len(transactions),
            "rowsImported": imported,
            "duplicates": duplicates,
            "metadata": {
                "skipped": skipped[:25],
                "skippedCount": len(skipped),
                "bankTransactionIds": bank_transaction_ids[:100],
            },
        })
        summary = {
            "success": True,
            "status": status,
            "bankStatementImportId": import_id,
            "accountIdentifier": account_identifier,
            "format": format,
            "source": source,
            "filename": filename,
            "rowsSeen": len(transactions),
            "rowsImported": imported,
            "duplicates": duplicates,
            "skipped": len(skipped),
            "bankTransactionIds": bank_transaction_ids,
            "externalSubmission": "not_executed",
        }
        self.ledger.record_audit_event({
            "action": "local_bank_transactions.import_completed",
            "entityType": "bank_statement_import",
            "entityId": str(import_id),
            "details": {
                "accountIdentifier": account_identifier,
                "format": format,
                "source": source,
                "actor": actor,
                "rowsSeen": summary["rowsSeen"],
                "rowsImported": imported,
                "duplicates": duplicates,
                "skipped": len(skipped),
                "externalSubmission": "not_executed",
            },
        })
        return summary

    def transactions_for_reconciliation(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.ledger.list_bank_transactions(
            reconciliation_status=OPEN_RECONCILIATION_STATUSES, limit=limit,
        )
        return [_transaction_for_reconciliation(row) for row in rows]


def normalize_bank_transaction(
    transaction: Dict[str, Any],
    account_identifier: str = "default",
    source: str = "manual_import",
) -> Dict[str, Any]:
    lookup = _Lookup(transaction)
    amount = _parse_amount(lookup)
    transaction_date = _normalize_date(
        lookup.first(
            "transactionDate",
            "transaction_date",
            "date",
            "datum",
            "bookingDate",
            "booking_date",
            "boekingsdatum",
            "booked",
            "valueDate",
            "value_date",
            "valutadatum",
        )
    )
    description = lookup.first(
        "description",
        "details",
        "memo",
        "remittanceInformation",
        "remittance_information",
        "omschrijving",
        "mededelingen",
    )
    counterparty = lookup.first(
        "counterparty",
        "vendor",
        "payee",
        "name",
        "merchant",
        "tegenpartij",
        "rekeningnaam",
        "naam tegenpartij",
        "naam_tegenpartij",
        "tegenrekeningnaam",
    )
    currency = str(
        lookup.first("currency", "ccy", "currencyCode", "currency_code", "valuta")
        or "EUR"
    ).strip() or "EUR"
    explicit_id = lookup.first(
        "transactionId",
        "transaction_id",
        "id",
        "reference",
        "ref",
        "accountServicerReference",
        "account_servicer_reference",
        "endToEndId",
        "end_to_end_id",
    )
    fingerprint = _fingerprint([
        account_identifier,
        transaction_date,
        amount,
        currency,
        description,
        counterparty,
    ])
    transaction_id = str(explicit_id or f"generated:{fingerprint[:16]}").strip()
    return {
        "accountIdentifier": account_identifier,
        "transactionId": transaction_id,
        "transactionDate": transaction_date,
        "amount": amount,
        "currency": currency,
        "description": str(description or "").strip() or None,
        "counterparty": str(counterparty or "").strip() or None,
        "status": "imported",
        "reconciliationStatus": "not_started",
        "duplicateFingerprint": fingerprint,
        "source": source,
        "metadata": {
            "generatedTransactionId": explicit_id in (None, ""),
            "sourceKeys": sorted(str(key) for key in transaction.keys()),
            "originalReference": explicit_id,
        },
    }


class _Lookup:
    def __init__(self, values: Dict[str, Any]):
        self.values = values
        self.normalized = {
            _key(key): value
            for key, value in values.items()
        }

    def first(self, *names: str) -> Any:
        for name in names:
            value = self.normalized.get(_key(name))
            if value is not None and value != "":
                return value
        return None


def _parse_csv_transactions(text: str) -> List[Dict[str, Any]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    normalized_headers = {_key(field) for field in (reader.fieldnames or []) if field}
    if not normalized_headers:
        raise ValueError("CSV bank statement must contain a header row")
    if not normalized_headers.intersection(CSV_DATE_FIELDS):
        raise ValueError("CSV bank statement must contain a recognized transaction date column")
    if not normalized_headers.intersection(CSV_AMOUNT_FIELDS):
        raise ValueError("CSV bank statement must contain a recognized amount, debit, or credit column")
    return [dict(row) for row in reader if row]


def _parse_camt_transactions(text: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("CAMT/XML bank statement is not valid XML") from exc
    transactions: List[Dict[str, Any]] = []
    for entry in _iter_local(root, "Ntry"):
        amount_text = _first_local_text(entry, "Amt")
        amount = _parse_number(amount_text)
        credit_debit = (_first_local_text(entry, "CdtDbtInd") or "").upper()
        if amount is not None and credit_debit.startswith("DBIT"):
            amount = -abs(amount)
        transaction = {
            "transactionDate": _first_path_text(entry, ("BookgDt", "Dt")) or _first_path_text(entry, ("ValDt", "Dt")),
            "amount": amount,
            "currency": _first_local_attribute(entry, "Amt", "Ccy") or "EUR",
            "description": _first_local_text(entry, "Ustrd") or _first_local_text(entry, "AddtlNtryInf"),
            "counterparty": _first_local_text(entry, "Nm"),
            "transactionId": (
                _first_local_text(entry, "AcctSvcrRef")
                or _first_local_text(entry, "NtryRef")
                or _first_local_text(entry, "EndToEndId")
            ),
        }
        transactions.append(transaction)
    return transactions


def _parse_mt940_transactions(text: str) -> List[Dict[str, Any]]:
    transactions: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(":61:"):
            if current:
                transactions.append(current)
            current = _parse_mt940_61(line, len(transactions) + 1)
        elif line.startswith(":86:") and current is not None:
            description = line[4:].strip()
            current["description"] = " ".join(
                part for part in [current.get("description"), description] if part
            )
        elif current is not None and line and not line.startswith(":"):
            current["description"] = " ".join(
                part for part in [current.get("description"), line] if part
            )
    if current:
        transactions.append(current)
    return transactions


def _parse_mt940_61(line: str, sequence: int) -> Dict[str, Any]:
    match = re.search(r":61:(\d{6})(?:\d{4})?([CD])(?:[A-Z])?([\d,.]+)", line)
    if not match:
        fingerprint = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
        return {"transactionId": f"mt940:{sequence}:{fingerprint}", "description": line}
    date_text, direction, amount_text = match.groups()
    amount = _parse_number(amount_text)
    if amount is not None and direction == "D":
        amount = -abs(amount)
    return {
        "transactionId": f"mt940:{sequence}:{hashlib.sha256(line.encode('utf-8')).hexdigest()[:12]}",
        "transactionDate": _mt940_date(date_text),
        "amount": amount,
        "currency": "EUR",
        "description": line,
    }


def _parse_amount(lookup: _Lookup) -> Optional[float]:
    value = lookup.first("amount", "transactionAmount", "transaction_amount", "bedrag", "value")
    if value is not None:
        return _parse_number(value)
    debit_value = lookup.first("debit", "af", "withdrawal")
    credit_value = lookup.first("credit", "bij", "deposit")
    debit = _parse_number(debit_value)
    credit = _parse_number(credit_value)
    if (debit_value is not None and debit is None) or (credit_value is not None and credit is None):
        return None
    if debit is None and credit is None:
        return None
    total = Decimal("0")
    if debit is not None:
        total -= abs(Decimal(str(debit)))
    if credit is not None:
        total += abs(Decimal(str(credit)))
    return finite_number(total)


def _parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return finite_number(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return finite_number(Decimal(text))
    except InvalidOperation:
        pass
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) <= 2 else "".join(parts)
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        number = -abs(number)
    return finite_number(number)


def _normalize_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _mt940_date(value: str) -> Optional[str]:
    if not value:
        return None
    year = int(value[:2])
    century = 2000 if year < 80 else 1900
    try:
        return date(century + year, int(value[2:4]), int(value[4:6])).isoformat()
    except ValueError:
        return value


def _transaction_for_reconciliation(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("transaction_id"),
        "transaction_id": row.get("transaction_id"),
        "ledgerBankTransactionId": row.get("id"),
        "ledger_bank_transaction_id": row.get("id"),
        "account_identifier": row.get("account_identifier"),
        "date": row.get("transaction_date"),
        "transaction_date": row.get("transaction_date"),
        "amount": row.get("amount"),
        "currency": row.get("currency"),
        "description": row.get("description"),
        "counterparty": row.get("counterparty"),
        "source": row.get("source"),
    }


def _has_financial_signal(transaction: Dict[str, Any]) -> bool:
    return (
        transaction.get("transactionDate") not in (None, "")
        and transaction.get("amount") is not None
    )


def _decode_statement_bytes(content: bytes) -> str:
    if b"\x00" in content:
        raise ValueError("Bank statement must be a text-based CSV, JSON, CAMT, or MT940 file")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = content.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError("Bank statement text encoding is not supported") from exc
    disallowed_controls = sum(
        1 for character in decoded
        if ord(character) < 32 and character not in {"\t", "\n", "\r"}
    )
    if disallowed_controls:
        raise ValueError("Bank statement must be a text-based CSV, JSON, CAMT, or MT940 file")
    return decoded


def _fingerprint(values: Iterable[Any]) -> str:
    return hashlib.sha256("|".join(str(value or "") for value in values).encode("utf-8")).hexdigest()


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _iter_local(root: ET.Element, local_name: str) -> Iterable[ET.Element]:
    for element in root.iter():
        if _local_name(element.tag) == local_name:
            yield element


def _first_local_text(root: ET.Element, local_name: str) -> Optional[str]:
    for element in root.iter():
        if _local_name(element.tag) == local_name and element.text:
            return element.text.strip()
    return None


def _first_path_text(root: ET.Element, path: Iterable[str]) -> Optional[str]:
    current = root
    for local_name in path:
        next_child = None
        for child in current:
            if _local_name(child.tag) == local_name:
                next_child = child
                break
        if next_child is None:
            return None
        current = next_child
    return current.text.strip() if current.text else None


def _first_local_attribute(root: ET.Element, local_name: str, attribute: str) -> Optional[str]:
    for element in root.iter():
        if _local_name(element.tag) == local_name:
            value = element.attrib.get(attribute)
            if value:
                return value
    return None


def _local_name(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, 500))
