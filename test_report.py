import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ledger import Contract, Payment, Work, read_ledger
from report import for_date

ROOT = Path(__file__).resolve().parent


class SuppliedDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts, cls.works, cls.payments = read_ledger(ROOT / "data")

    def report_at(self, cutoff):
        reports = for_date(
            self.contracts.values(), self.works.values(), self.payments.values(), date.fromisoformat(cutoff)
        )
        return {report.contract.id: report for report in reports}

    def test_control_date_and_reconciliation(self):
        reports = self.report_at("2026-11-10")
        for contract, expected_paid, expected_unallocated in (
            ("D-1", 31_500_000, 1_500_000),
            ("D-2", 7_500_000, 2_500_000),
        ):
            with self.subTest(contract=contract):
                report = reports[contract]
                self.assertEqual(sum(row.included for row in report.payments), expected_paid)
                self.assertEqual(report.unallocated, expected_unallocated)
                self.assertEqual(sum(row.paid for row in report.works), expected_paid - expected_unallocated)
                accepted = [row for row in report.works if row.status == "принята"]
                self.assertTrue(accepted)
                self.assertTrue(all(row.due == 0 for row in accepted))

        work14 = next(row for row in reports["D-1"].works if row.work.id == "W-14")
        self.assertEqual((work14.direct, work14.from_pool), (1_500_000, 8_500_000))
        work23 = next(row for row in reports["D-2"].works if row.work.id == "W-23")
        self.assertEqual((work23.status, work23.paid, work23.due), ("запланирована", 0, None))
        self.assertEqual(
            {row.payment.id: row.included for row in reports["D-1"].payments},
            {"P-1": 30_000_000, "P-2": 0, "P-3": 1_500_000, "P-4": 0},
        )

    def test_signing_payment_refund_and_cancellation_are_inclusive(self):
        self.assertEqual(self.report_at("2026-08-24"), {})
        self.assertEqual(list(self.report_at("2026-08-25")), ["D-1"])
        for cutoff, expected in (("2026-08-31", 0), ("2026-09-01", 30_000_000),
                                 ("2026-09-09", 35_000_000), ("2026-09-10", 30_000_000)):
            with self.subTest(cutoff=cutoff):
                report = self.report_at(cutoff)["D-1"]
                self.assertEqual(sum(row.included for row in report.payments), expected)

        before = self.report_at("2026-10-11")["D-1"]
        after = self.report_at("2026-10-12")["D-1"]
        self.assertEqual(
            [next(row.status for row in report.works if row.work.id == "W-13") for report in (before, after)],
            ["запланирована", "отменена"],
        )

    def test_acceptance_and_payment_next_days(self):
        expected = (
            ("2026-11-11", 2_500_000, 1_500_000, 0),
            ("2026-11-12", 4_000_000, 0, 500_000),
        )
        for cutoff, paid, due, unallocated in expected:
            with self.subTest(cutoff=cutoff):
                report = self.report_at(cutoff)["D-2"]
                work = next(row for row in report.works if row.work.id == "W-23")
                self.assertEqual(work.status, "принята")
                self.assertEqual((work.paid, work.due, report.unallocated), (paid, due, unallocated))

    def test_cli_explains_actual_input_from_another_directory(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "report.py"), "--date", "2026-11-10", "--explain"],
                cwd=cwd, capture_output=True, text=True, check=True,
            )
        self.assertIn("315 000,00 = 300 000,00 по работам + 15 000,00 не распределено", result.stdout)
        self.assertIn("P-2: 50 000,00 → 0,00", result.stdout)
        self.assertIn("W-14 (2026-10-28): 100 000,00 − 85 000,00 = 15 000,00", result.stdout)


class EdgeCasesTest(unittest.TestCase):
    def test_ties_only_allocate_available_cents_and_keep_explicit_overpayments(self):
        cutoff = date(2026, 1, 1)
        contract = Contract("D", "Подрядчик", 900, cutoff)
        works = [
            Work("W-3", "D", "Прямая переплата", 100, cutoff, None),
            Work("W-2", "D", "Вторая", 100, cutoff, None),
            Work("W-4", "D", "Отмена", 100, None, cutoff),
            Work("W-1", "D", "Первая", 100, cutoff, None),
        ]
        payments = [
            Payment("P-3", "D", "W-4", 5, cutoff, None),
            Payment("P-2", "D", "W-3", 107, cutoff, None),
            Payment("P-1", "D", "", 133, cutoff, None),
        ]
        report, = for_date([contract], works, payments, cutoff)
        self.assertEqual(
            [(row.work.id, row.direct, row.from_pool, row.due) for row in report.works],
            [("W-1", 0, 100, 0), ("W-2", 0, 33, 67), ("W-3", 107, 0, 0), ("W-4", 5, 0, None)],
        )
        self.assertEqual(report.unallocated, 0)
        self.assertEqual(sum(row.paid for row in report.works), 245)

    def test_csv_amounts_are_exact_and_invalid_links_fail_without_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)

            def write(name, *rows):
                with (data / name).open("w", encoding="utf-8", newline="") as stream:
                    csv.writer(stream).writerows(rows)

            write("contracts.csv", ("contract_id", "contractor", "amount", "signed_at"),
                  ("D-1", "Подрядчик, ИП", "0.30", "2026-01-01"), ("D-2", "Второй", "0.30", "2026-01-01"))
            write("works.csv", ("work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"),
                  ("W", "D-1", "Работа", "0.28", "done", "2026-01-01", ""))
            headers = ("payment_id", "contract_id", "work_id", "purpose", "amount", "status", "created_at",
                       "paid_at", "refunded_at")
            direct = ("P-1", "D-1", "W", "", "0.10", "paid", "2026-01-01", "2026-01-01", "")
            pool = ("P-2", "D-1", "", "", "0.20", "paid", "2026-01-01", "2026-01-01", "")
            write("payments.csv", headers, direct, pool)
            contracts, works, payments = read_ledger(data)
            first, second = for_date(contracts.values(), works.values(), payments.values(), date(2026, 1, 1))
            self.assertEqual((first.works[0].paid, first.unallocated), (28, 2))
            self.assertEqual(second.unallocated, 0)

            for replacement, expected in (
                (("P-2", "D-2", "W", "", *pool[4:]), "работа W отсутствует в договоре D-2"),
                ((*pool[:4], "0.001", *pool[5:]), "сумма до копеек"),
            ):
                with self.subTest(replacement=replacement):
                    write("payments.csv", headers, direct, replacement)
                    result = subprocess.run(
                        [sys.executable, "-B", str(ROOT / "report.py"), "--date", "2026-01-01",
                         "--data-dir", str(data)], capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
