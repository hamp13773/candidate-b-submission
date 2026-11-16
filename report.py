"""Отчёт о выплатах по договорам на заданную дату."""

import argparse
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ledger import Contract, Payment, Work, parse_date, read_ledger


@dataclass(frozen=True)
class WorkRow:
    work: Work
    status: str
    direct: int
    from_pool: int

    @property
    def paid(self) -> int:
        return self.direct + self.from_pool

    @property
    def due(self) -> int | None:
        return max(0, self.work.price - self.paid) if self.status == "принята" else None


@dataclass(frozen=True)
class PaymentRow:
    payment: Payment
    included: int
    reason: str


@dataclass(frozen=True)
class ContractReport:
    contract: Contract
    works: list[WorkRow]
    payments: list[PaymentRow]
    unallocated: int


def calculate(contract: Contract, works: list[Work], payments: list[Payment], cutoff: date) -> ContractReport:
    direct: dict[str, int] = defaultdict(int)
    pool = 0
    payment_rows = []
    for payment in sorted(payments, key=lambda item: item.id):
        amount, reason = payment.effect_on(cutoff)
        payment_rows.append(PaymentRow(payment, amount, reason))
        if payment.work_id:
            direct[payment.work_id] += amount
        else:
            pool += amount

    accepted = sorted(
        (work for work in works if work.status_on(cutoff) == "принята"),
        key=lambda work: (work.completed_at, work.id),
    )
    allocations = {}
    for work in accepted:
        remaining_price = max(0, work.price - direct[work.id])
        allocations[work.id] = min(pool, remaining_price)
        pool -= allocations[work.id]

    work_rows = [
        WorkRow(work, work.status_on(cutoff), direct[work.id], allocations.get(work.id, 0))
        for work in sorted(works, key=lambda item: item.id)
    ]
    return ContractReport(contract, work_rows, payment_rows, pool)


def for_date(
    contracts: Iterable[Contract], works: Iterable[Work], payments: Iterable[Payment], cutoff: date
) -> list[ContractReport]:
    works_by_contract: dict[str, list[Work]] = defaultdict(list)
    payments_by_contract: dict[str, list[Payment]] = defaultdict(list)
    for work in works:
        works_by_contract[work.contract_id].append(work)
    for payment in payments:
        payments_by_contract[payment.contract_id].append(payment)
    return [
        calculate(contract, works_by_contract[contract.id], payments_by_contract[contract.id], cutoff)
        for contract in sorted(contracts, key=lambda item: item.id)
        if contract.signed_at <= cutoff
    ]


def money(amount: int) -> str:
    rubles, cents = divmod(amount, 100)
    return f"{rubles:,}".replace(",", " ") + f",{cents:02d}"


def table(headings: tuple[str, ...], rows: list[tuple[str, ...]], numeric_from: int) -> str:
    widths = [max(map(len, column)) for column in zip(headings, *rows)]
    return "\n".join(
        "  ".join(
            value.rjust(width) if index >= numeric_from else value.ljust(width)
            for index, (value, width) in enumerate(zip(row, widths))
        ).rstrip()
        for row in (headings, *rows)
    )


def render_contract(report: ContractReport, explain: bool) -> str:
    contract = report.contract
    observed_prices: dict[str, int] = defaultdict(int)
    for row in report.works:
        observed_prices[row.status] += row.work.price

    lines = [
        f"{contract.id} · {contract.contractor}",
        f"Сумма договора: {money(contract.amount)}",
        "",
    ]
    if report.works:
        lines.append(table(
            ("Работа", "Статус", "Цена", "Напрямую", "Зачтено", "Выплачено", "Осталось оплатить"),
            [
                (
                    f"{row.work.id} {row.work.title}",
                    row.status,
                    money(row.work.price),
                    money(row.direct),
                    money(row.from_pool),
                    money(row.paid),
                    "—" if row.due is None else money(row.due),
                )
                for row in report.works
            ],
            numeric_from=2,
        ))
    else:
        lines.append("Работ в выгрузке нет.")

    net_paid = sum(row.included for row in report.payments)
    assigned = sum(row.paid for row in report.works)
    due = sum(row.due for row in report.works if row.due is not None)
    lines.extend((
        "",
        f"Стоимость работ: принято {money(observed_prices['принята'])}, "
        f"запланировано {money(observed_prices['запланирована'])}, "
        f"отменено {money(observed_prices['отменена'])}.",
        f"Выплачено за вычетом возвратов: {money(net_paid)} = "
        f"{money(assigned)} по работам + {money(report.unallocated)} не распределено.",
        f"Осталось оплатить за принятые работы: {money(due)}.",
    ))
    if explain:
        lines.extend(("", "Платежи (сумма в выгрузке → учтено в отчёте)"))
        for row in report.payments:
            payment = row.payment
            target = payment.work_id or "общий остаток договора"
            lines.append(
                f"  {payment.id}: {money(payment.amount)} → {money(row.included)}; {target}; {row.reason}."
            )
        if not report.payments:
            lines.append("  Платежей в выгрузке нет.")

        lines.append("Зачёт общего остатка (по дате приёмки, затем по ID)")
        allocated = sorted(
            (row for row in report.works if row.from_pool),
            key=lambda row: (row.work.completed_at, row.work.id),
        )
        remainder = report.unallocated + sum(row.from_pool for row in allocated)
        for row in allocated:
            available = remainder
            remainder -= row.from_pool
            lines.append(
                f"  {row.work.id} ({row.work.completed_at}): "
                f"{money(available)} − {money(row.from_pool)} = {money(remainder)}."
            )
        if not allocated:
            lines.append("  Зачётов нет.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Отчёт по выплатам на дату включительно.")
    parser.add_argument("--date", required=True, metavar="ГГГГ-ММ-ДД", help="дата отчёта")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parent / "data",
        help="папка с исходными CSV (по умолчанию data рядом со скриптом)",
    )
    parser.add_argument("--explain", action="store_true", help="показать сверку платежей и распределение")
    args = parser.parse_args()
    try:
        cutoff = parse_date(args.date)
        contracts, works, payments = read_ledger(args.data_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    reports = for_date(contracts.values(), works.values(), payments.values(), cutoff)
    print(f"Отчёт на {cutoff:%d.%m.%Y} включительно · суммы в рублях\n")
    if not reports:
        print("На эту дату подписанных договоров нет.")
    else:
        print("\n\n".join(render_contract(report, args.explain) for report in reports))


if __name__ == "__main__":
    main()
