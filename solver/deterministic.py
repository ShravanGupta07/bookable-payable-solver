"""solver/deterministic.py — Deterministic Normalization & Grounded Patching.

Implements the 'Determinism Boundary at Ambiguity' principle (B3):
- Provable / math-certain / domain-universal invariants are enforced deterministically here.
- Ambiguous / world-knowledge / document-interpretation questions are surfaced as Signals to the LLM.
"""
from __future__ import annotations
import re
from typing import Any
import erp
from solver.master_index import MasterDataIndex


def normalize_dot_decimal(val: Any) -> str:
    """Deterministically normalize locale-formatted numbers (e.g. '1.234,56' or '1 234,56')
    into plain dot-decimal strings expected by erp.py."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # Strip currency symbols and spaces
    s = re.sub(r"[€$£¥₹%\s\u00a0]", "", s)
    if not s:
        return ""
    # Handle European vs Anglo thousand/decimal separators
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # 1.234,56 -> 1234.56
            s = s.replace(".", "").replace(",", ".")
        else:
            # 1,234.56 -> 1234.56
            s = s.replace(",", "")
    elif "," in s:
        # Check if comma is decimal separator (e.g. 123,45 or 12,5) vs thousands (1,000)
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 3, 4):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        float(s)
        return s
    except ValueError:
        return ""


def apply_deterministic_invariants(
    payable: dict[str, Any],
    master_idx: MasterDataIndex,
) -> dict[str, Any]:
    """Enforce math-certain schema invariants and deterministic master-data enrichment."""
    p = dict(payable)

    # Alias normalization for common LLM field variations
    if not p.get("gross_total"):
        p["gross_total"] = p.get("total_amount") or p.get("invoice_total") or p.get("total") or p.get("amount_due") or ""
    if not p.get("subtotal"):
        p["subtotal"] = p.get("net_total") or p.get("net_amount") or p.get("sub_total") or ""
    if not p.get("total_tax_amount"):
        p["total_tax_amount"] = p.get("tax_total") or p.get("tax_amount") or p.get("total_tax") or ""
    if not p.get("po_number"):
        p["po_number"] = p.get("purchase_order") or p.get("po_no") or p.get("po_id") or ""
    if not p.get("invoice_number"):
        p["invoice_number"] = p.get("invoice_no") or p.get("invoice_id") or p.get("number") or ""
    if not p.get("currency"):
        p["currency"] = p.get("doc_currency") or p.get("invoice_currency") or ""

    # 1. Normalize numeric fields to dot-decimal
    for k in (
        "gross_total", "subtotal", "total_tax_amount", "discount_amount",
        "freight_charges", "insurance_charges", "extra_charges", "excise_duties"
    ):
        p[k] = normalize_dot_decimal(p.get(k, ""))

    is_credit = (str(p.get("invoice_type") or "").upper().strip() == "CREDIT_MEMO")
    if is_credit:
        p["invoice_type"] = "CREDIT_MEMO"
        # Credit memos use positive magnitudes in the schema
        for k in ("gross_total", "subtotal", "total_tax_amount", "discount_amount"):
            if p.get(k) and p[k].startswith("-"):
                p[k] = p[k][1:]
    else:
        p["invoice_type"] = "INVOICE"

    # 2. Deterministic Master-Data Resolution (Rule 2: real match or "")
    supp = dict(p.get("supplier") or {})
    if not supp.get("name"):
        supp["name"] = p.get("supplier_name") or p.get("vendor_name") or ""
    if not supp.get("vat_id"):
        supp["vat_id"] = p.get("supplier_tax_id") or p.get("vendor_tax_id") or p.get("vat_id") or ""
    if not supp.get("address"):
        supp["address"] = p.get("supplier_address") or p.get("vendor_address") or ""

    resolved_sid = master_idx.resolve_supplier(
        name=supp.get("name", ""),
        vat_id=supp.get("vat_id", ""),
        iban=supp.get("bank_iban", ""),
        email=supp.get("email", ""),
    )
    supp["supplier_id"] = resolved_sid
    p["supplier"] = supp

    # Preserve flat fields for schema compatibility
    p["supplier_name"] = supp.get("name", "")
    p["supplier_tax_id"] = supp.get("vat_id", "")
    p["supplier_address"] = supp.get("address", "")
    p["supplier_id"] = resolved_sid

    # Resolve PO against po_master.json (non-ERP references must leave po_id == "")
    raw_po = str(p.get("po_number") or "").strip()
    resolved_poid = master_idx.resolve_po(raw_po)
    p["po_id"] = resolved_poid
    p["po_number"] = raw_po

    # Resolve Buyer against chart_of_books.json
    buyer = dict(p.get("buyer") or {})
    if not buyer.get("name"):
        buyer["name"] = p.get("customer_name") or p.get("buyer_name") or ""
    if not buyer.get("address"):
        buyer["address"] = p.get("customer_address") or p.get("buyer_address") or ""
    if not buyer.get("tax_id"):
        buyer["tax_id"] = p.get("customer_tax_id") or p.get("buyer_tax_id") or ""

    if not buyer.get("business_unit_code"):
        resolved_buyer = master_idx.resolve_buyer(
            buyer_name=buyer.get("name", "") or buyer.get("business_unit_name", ""),
            buyer_address=buyer.get("address", ""),
            currency=p.get("currency", ""),
        )
        buyer.update({k: v for k, v in resolved_buyer.items() if v})
    p["buyer"] = {
        "company_code": buyer.get("company_code", "BOLTGROUP") or "BOLTGROUP",
        "business_unit_code": buyer.get("business_unit_code", ""),
        "location_code": buyer.get("location_code", ""),
    }
    p["customer_name"] = buyer.get("name", "")
    p["customer_tax_id"] = buyer.get("tax_id", "")
    p["customer_address"] = buyer.get("address", "")
    p["company_code"] = p["buyer"]["company_code"]
    p["business_unit_code"] = p["buyer"]["business_unit_code"]
    p["location_code"] = p["buyer"]["location_code"]

    # Resolve Payment Term
    if not p.get("payment_term_id") or p["payment_term_id"] not in {
        pt["payment_term_id"] for pt in master_idx.payment_terms
    }:
        p["payment_term_id"] = master_idx.resolve_payment_term(
            raw_term=str(p.get("payment_term_id") or p.get("payment_terms") or p.get("_raw_payment_term") or ""),
            invoice_date=str(p.get("invoice_date") or ""),
            due_date=str(p.get("due_date") or ""),
        )

    # Flat totals compatibility
    p["net_total"] = p.get("subtotal", "")
    p["tax_total"] = p.get("total_tax_amount", "")

    # Infer supplier country for tax resolution
    supp_country = ""
    if resolved_sid and resolved_sid in master_idx.suppliers_by_id:
        supp_country = master_idx.suppliers_by_id[resolved_sid].get("country", "")
    elif supp.get("vat_id") and len(supp["vat_id"]) >= 2 and supp["vat_id"][:2].isalpha():
        supp_country = supp["vat_id"][:2].upper()

    # 3. Line Items: dot-decimal, positive credit magnitudes, and provable pack-scale fix
    norm_lines = []
    for li in (p.get("line_items") or []):
        li = dict(li)
        # Normalize line-level aliases
        if not li.get("total"):
            li["total"] = li.get("line_total") or li.get("net_total") or li.get("net_amount") or li.get("amount") or ""
        if not li.get("description"):
            li["description"] = li.get("item_description") or li.get("name") or ""
        if not li.get("item_code"):
            li["item_code"] = li.get("item_no") or li.get("product_code") or li.get("code") or ""
        if not li.get("unit_price"):
            li["unit_price"] = li.get("price") or ""

        for nk in ("quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount"):
            li[nk] = normalize_dot_decimal(li.get(nk, ""))
            if is_credit and nk in ("quantity", "unit_price", "total", "tax_amount") and li[nk].startswith("-"):
                li[nk] = li[nk][1:]

        # Flat net_total preservation for schema compatibility
        li["net_total"] = li["total"]

        # Grounded deterministic patch: if qty * unit_price is exactly 100x or 1000x the printed line total
        qty = erp.num(li.get("quantity"))
        uprice = erp.num(li.get("unit_price"))
        ltotal = erp.num(li.get("total"))
        disc_pct = abs(erp.num(li.get("discount_percentage")))
        disc_amt = abs(erp.num(li.get("discount")))
        if qty > 0 and uprice > 0 and ltotal > 0 and disc_pct == 0 and disc_amt == 0:
            raw_ext = qty * uprice
            for scale in (100.0, 1000.0, 10.0):
                if abs((raw_ext / scale) - ltotal) <= 0.02:
                    li["unit_price"] = f"{uprice / scale:.6f}".rstrip("0").rstrip(".")
                    break

        # Normalize per-line taxes[]
        ltaxes = []
        raw_ltaxes = li.get("taxes") or []
        if not raw_ltaxes and (li.get("tax_rate") or li.get("tax_amount")):
            raw_ltaxes = [{"tax_rate": li.get("tax_rate", ""), "tax_amount": li.get("tax_amount", "")}]

        for t in raw_ltaxes:
            t = dict(t)
            t["tax_rate"] = normalize_dot_decimal(t.get("tax_rate", ""))
            t["tax_amount"] = normalize_dot_decimal(t.get("tax_amount", ""))
            if is_credit and t["tax_amount"].startswith("-"):
                t["tax_amount"] = t["tax_amount"][1:]
            if not t.get("tax_type_code"):
                t["tax_type_code"] = master_idx.resolve_tax_code(
                    rate=erp.num(t["tax_rate"]),
                    country=supp_country,
                    tax_name=t.get("tax_name", ""),
                    is_withholding=(erp.num(t["tax_amount"]) < 0),
                )
            ltaxes.append(t)
        li["taxes"] = ltaxes
        norm_lines.append(li)
    p["line_items"] = norm_lines

    # Duplicate discount protection: if header discount duplicates line discounts, zero header
    hdr_disc = erp.num(p.get("discount_amount"))
    if hdr_disc > 0:
        line_disc_sum = sum(erp.num(li.get("discount")) for li in norm_lines)
        if abs(line_disc_sum - hdr_disc) <= 0.05 or any(abs(erp.num(li.get("discount")) - hdr_disc) <= 0.05 for li in norm_lines):
            p["discount_amount"] = "0.00"

    # 4. Header Taxes: dot-decimal and tax_type_code resolution
    htaxes = []
    for t in (p.get("taxes") or []):
        t = dict(t)
        t["tax_rate"] = normalize_dot_decimal(t.get("tax_rate", ""))
        t["tax_amount"] = normalize_dot_decimal(t.get("tax_amount", ""))
        is_wht = (
            "WITHHOLD" in str(t.get("tax_name", "")).upper()
            or str(t.get("tax_type", "")).upper() == "WHT"
            or "หัก" in str(t.get("tax_name", ""))
            or "หัก" in str(t.get("tax_type", ""))
        )
        if is_wht and erp.num(t["tax_amount"]) > 0:
            t["tax_amount"] = f"-{t['tax_amount']}"
        elif is_credit and not is_wht and t["tax_amount"].startswith("-"):
            t["tax_amount"] = t["tax_amount"][1:]
        if not t.get("tax_type_code"):
            t["tax_type_code"] = master_idx.resolve_tax_code(
                rate=erp.num(t["tax_rate"]),
                country=supp_country,
                tax_name=t.get("tax_name", ""),
                is_withholding=is_wht or (erp.num(t["tax_amount"]) < 0),
            )
        htaxes.append(t)

    p["taxes"] = htaxes

    return p