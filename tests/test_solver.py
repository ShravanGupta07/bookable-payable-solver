"""tests/test_solver.py — Unit tests for the risky solver boundaries and invariants."""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import erp
from solver.deterministic import apply_deterministic_invariants, normalize_dot_decimal
from solver.master_index import MasterDataIndex
from solver.signals import diagnose_payable
from solver.agent_loop import solve_document

MASTER_DIR = os.path.join(HERE, "master_data")

def test_locale_number_normalization():
    assert normalize_dot_decimal("1.234,56") == "1234.56"
    assert normalize_dot_decimal("1,234.56") == "1234.56"
    assert normalize_dot_decimal("€ 438,00") == "438.00"
    assert normalize_dot_decimal("-235,44") == "-235.44"


def test_pack_scaling_deterministic_patch_without_corrupting_normal_lines():
    idx = MasterDataIndex(MASTER_DIR)
    # Pack-scaled line (DU-05 style: qty * unit_price is 100x printed line total)
    scaled_payable = {
        "invoice_type": "INVOICE",
        "currency": "SGD",
        "gross_total": "771.66",
        "line_items": [
            {"quantity": "100", "unit_price": "771.66", "total": "771.66"}
        ],
    }
    patched = apply_deterministic_invariants(scaled_payable, idx)
    assert erp.erp_book(patched)["will_book_gross"] == 771.66

    # Control twin: normal line where qty * unit_price already equals total
    normal_payable = {
        "invoice_type": "INVOICE",
        "currency": "EUR",
        "gross_total": "292.00",
        "line_items": [
            {"quantity": "4", "unit_price": "73.00", "total": "292.00"}
        ],
    }
    patched_norm = apply_deterministic_invariants(normal_payable, idx)
    assert patched_norm["line_items"][0]["unit_price"] == "73.00"
    assert erp.erp_book(patched_norm)["will_book_gross"] == 292.00


def test_withholding_tax_negative_sign_in_erp():
    idx = MasterDataIndex(MASTER_DIR)
    payable = {
        "invoice_type": "INVOICE",
        "currency": "THB",
        "gross_total": "8161.92",
        "extra_charges": "648.00",
        "supplier": {"name": "Thai Agency", "vat_id": "TH123"},
        "taxes": [
            {"tax_type": "VAT", "tax_name": "VAT 7%", "tax_rate": "7", "tax_amount": "549.36"},
            {"tax_type": "WHT", "tax_name": "Withholding Tax 3%", "tax_rate": "3", "tax_amount": "235.44"},
        ],
        "line_items": [
            {"quantity": "1", "unit_price": "7200.00", "total": "7200.00"}
        ],
    }
    patched = apply_deterministic_invariants(payable, idx)
    # WHT must be negative so erp_book deducts it
    assert patched["taxes"][1]["tax_amount"] == "-235.44"
    assert erp.erp_book(patched)["will_book_gross"] == 8161.92


def test_duplicate_line_and_header_tax_emits_signal():
    payable = {
        "invoice_type": "CREDIT_MEMO",
        "currency": "GBP",
        "gross_total": "120.00",
        "total_tax_amount": "20.00",
        "taxes": [{"tax_rate": "20", "tax_amount": "20.00"}],
        "line_items": [
            {
                "quantity": "1",
                "unit_price": "100.00",
                "total": "100.00",
                "taxes": [{"tax_rate": "20", "tax_amount": "20.00"}],
            }
        ],
    }
    sigs = diagnose_payable(payable)
    codes = {s.code for s in sigs}
    assert "TAX_AT_BOTH_LEVELS" in codes
    assert "DELTA_EQUALS_TOTAL_TAX" in codes


def test_honest_blank_on_unmatched_master_data():
    idx = MasterDataIndex(MASTER_DIR)
    # Unknown supplier & non-ERP PO must resolve to "" (Rule 2)
    assert idx.resolve_supplier(name="Completely Unknown ZAR Vendor Pty Ltd", vat_id="ZA0000000") == ""
    assert idx.resolve_po("YOUR-REF-998877") == ""
    # Known supplier resolves via O(1) VAT index
    assert idx.resolve_supplier(name="Phocus", vat_id="DE209177122") == "2845695"


def test_agent_loop_converges_and_escalates_on_signals():
    idx = MasterDataIndex(MASTER_DIR)
    calls = []

    def mock_llm(messages, model_name):
        calls.append((model_name, len(messages)))
        if len(calls) == 1:
            # Turn 1: emits duplicate tax at both line and header
            return {
                "file": "TEST.pdf",
                "payables": [{
                    "invoice_type": "INVOICE",
                    "currency": "EUR",
                    "gross_total": "120.00",
                    "total_tax_amount": "20.00",
                    "taxes": [{"tax_rate": "20", "tax_amount": "20.00"}],
                    "line_items": [{
                        "quantity": "1",
                        "unit_price": "100.00",
                        "total": "100.00",
                        "taxes": [{"tax_rate": "20", "tax_amount": "20.00"}],
                    }],
                }],
                "declined": [],
            }
        # Turn 2: repairs duplicate tax after receiving TAX_AT_BOTH_LEVELS signal
        return {
            "file": "TEST.pdf",
            "payables": [{
                "invoice_type": "INVOICE",
                "currency": "EUR",
                "gross_total": "120.00",
                "total_tax_amount": "20.00",
                "taxes": [{"tax_rate": "20", "tax_amount": "20.00"}],
                "line_items": [{
                    "quantity": "1",
                    "unit_price": "100.00",
                    "total": "100.00",
                    "taxes": [],
                }],
            }],
            "declined": [],
        }

    out = solve_document("TEST", "Invoice 100 + 20 VAT = 120 EUR", idx, mock_llm, "fast-m", "strong-m")
    assert len(calls) == 2
    assert calls[0][0] == "fast-m"
    assert calls[1][0] == "strong-m"
    assert erp.erp_book(out["payables"][0])["will_book_gross"] == 120.00