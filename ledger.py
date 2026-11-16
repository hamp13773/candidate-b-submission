"""Чтение и проверка исходной выгрузки."""

import csv
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar


@dataclass(frozen=True)
class Contract:
    id: str
    contractor: str
    amount: int
    signed_at: date


@dataclass(frozen=True)
class Work:
    id: str
    contract_id: str
    title: str
    price: int
    completed_at: date | None
    cancelled_at: date | None

    def status_on(self, cutoff: date) -> str:
        if self.cancelled_at is not None and self.cancelled_at <= cutoff:
            return "отменена"
        if self.completed_at is not None and self.completed_at <= cutoff:
            return "принята"
        return "запланирована"


@dataclass(frozen=True)
class Payment:
    id: str
    contract_id: str
    work_id: str
    amount: int
    paid_at: date | None
    refunded_at: date | None

    def effect_on(self, cutoff: date) -> tuple[int, str]:
        if self.paid_at is None:
            return 0, "в очереди: оплата не состоялась"
        if self.paid_at > cutoff:
            return 0, f"оплачен после даты отчёта ({self.paid_at})"
        if self.refunded_at is not None and self.refunded_at <= cutoff:
            return 0, f"возвращён {self.refunded_at}"
        return self.amount, f"оплачен {self.paid_at}"


def parse_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"ожидалась дата ГГГГ-ММ-ДД, получено {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"несуществующая дата {value!r}") from None


def optional_date(value: str) -> date | None:
    return parse_date(value) if value else None


def kopecks(value: str) -> int:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError(f"ожидалась неотрицательная сумма до копеек, получено {value!r}")
    rubles, _, fraction = value.partition(".")
    return int(rubles) * 100 + int(fraction.ljust(2, "0"))


def rows(path: Path, required: str) -> Iterator[tuple[str, dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        try:
            headers = reader.fieldnames or []
            missing = set(required.split()) - set(headers)
            if missing:
                raise ValueError(f"{path.name}: нет колонок {', '.join(sorted(missing))}")
            if len(headers) != len(set(headers)):
                raise ValueError(f"{path.name}: названия колонок повторяются")
            for row in reader:
                location = f"{path.name}:{reader.line_num}"
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{location}: число полей не совпадает с заголовком")
                yield location, {key: value.strip() for key, value in row.items()}
        except csv.Error as error:
            raise ValueError(f"{path.name}:{reader.line_num}: {error}") from error


def required_field(row: dict[str, str], name: str) -> str:
    if not row[name]:
        raise ValueError(f"пустое поле {name}")
    return row[name]


def parse_contract(row: dict[str, str]) -> Contract:
    return Contract(
        required_field(row, "contract_id"),
        required_field(row, "contractor"),
        kopecks(row["amount"]),
        parse_date(row["signed_at"]),
    )


def parse_work(row: dict[str, str]) -> Work:
    status = row["status"]
    completed = optional_date(row["completed_at"])
    cancelled = optional_date(row["cancelled_at"])
    if status not in {"planned", "done", "cancelled"}:
        raise ValueError(f"неизвестный статус работы {status!r}")
    if (status == "cancelled") != (cancelled is not None):
        raise ValueError("статус работы не согласуется с датой отмены")
    if (status == "done" and completed is None) or (status == "planned" and completed is not None):
        raise ValueError("статус работы не согласуется с датой приёмки")
    if completed is not None and cancelled is not None and cancelled < completed:
        raise ValueError("отмена раньше приёмки")
    return Work(
        required_field(row, "work_id"),
        required_field(row, "contract_id"),
        required_field(row, "title"),
        kopecks(row["price"]),
        completed,
        cancelled,
    )


def parse_payment(row: dict[str, str]) -> Payment:
    status = row["status"]
    parse_date(row["created_at"])
    paid = optional_date(row["paid_at"])
    refunded = optional_date(row["refunded_at"])
    if status not in {"queued", "paid", "refunded"}:
        raise ValueError(f"неизвестный статус платежа {status!r}")
    if (status == "queued") != (paid is None):
        raise ValueError("статус платежа не согласуется с датой выплаты")
    if (status == "refunded") != (refunded is not None):
        raise ValueError("статус платежа не согласуется с датой возврата")
    if refunded is not None and paid is not None and refunded < paid:
        raise ValueError("возврат раньше выплаты")
    return Payment(
        required_field(row, "payment_id"),
        required_field(row, "contract_id"),
        row["work_id"],
        kopecks(row["amount"]),
        paid,
        refunded,
    )


Record = TypeVar("Record", Contract, Work, Payment)


def read_unique(
    path: Path, fields: str, parse: Callable[[dict[str, str]], Record]
) -> dict[str, Record]:
    records: dict[str, Record] = {}
    for location, row in rows(path, fields):
        try:
            record = parse(row)
            if record.id in records:
                raise ValueError(f"повторяется ID {record.id}")
        except ValueError as error:
            raise ValueError(f"{location}: {error}") from error
        records[record.id] = record
    return records


def read_ledger(data_dir: Path) -> tuple[dict[str, Contract], dict[str, Work], dict[str, Payment]]:
    contracts = read_unique(data_dir / "contracts.csv", "contract_id contractor amount signed_at", parse_contract)
    works = read_unique(
        data_dir / "works.csv",
        "work_id contract_id title price status completed_at cancelled_at",
        parse_work,
    )
    payments = read_unique(
        data_dir / "payments.csv",
        "payment_id contract_id work_id amount status created_at paid_at refunded_at",
        parse_payment,
    )
    for work in works.values():
        if work.contract_id not in contracts:
            raise ValueError(f"works.csv: {work.id}: неизвестный договор {work.contract_id}")
    for payment in payments.values():
        if payment.contract_id not in contracts:
            raise ValueError(f"payments.csv: {payment.id}: неизвестный договор {payment.contract_id}")
        if payment.work_id:
            work = works.get(payment.work_id)
            if work is None or work.contract_id != payment.contract_id:
                raise ValueError(
                    f"payments.csv: {payment.id}: работа {payment.work_id} "
                    f"отсутствует в договоре {payment.contract_id}"
                )
    return contracts, works, payments
