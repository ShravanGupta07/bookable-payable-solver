"""solver/prompt_cache.py — Cache-Friendly Prompt Assembly & Positional Attention Layout.

Engineering design (B1):
1. STATIC_SYSTEM_PREFIX is 100% invariant across all documents and repair turns,
   enabling full KV prompt-cache hits on the system prefix.
2. Lost-in-the-middle mitigation: High-salience invariants sit at the TOP of the
   context, raw document evidence and master reference sit in the MIDDLE, and the
   active turn directive / diagnostic Signals sit at the BOTTOM (recency extreme).
"""
from __future__ import annotations
import json
from typing import Any, List
from solver.signals import Signal

# ── 100% STATIC CACHEABLE SYSTEM PREFIX (Never interpolate per-doc variables here!) ──
STATIC_SYSTEM_PREFIX = """You are an expert Accounts Payable (AP) Clerk and Solver.
Your job is to translate supplier PDFs into bookable ERP autodraft records that satisfy the sealed ERP recompute contract (erp.py).

### GOVERNING INVARIANTS (TOP-OF-CONTEXT ANCHOR)
1. PRESENTATION != ERP MODEL:
   - erp.py computes each line's net base as: round2(quantity * unit_price - discount)
   - erp.py computes gross as: round2(sum(line_bases) - discount_amount + sum(line_taxes) + sum(header_taxes) + freight_charges + insurance_charges + extra_charges + excise_duties)
   - Therefore, `unit_price` MUST be NET (tax-exclusive) and scaled per-1-unit of `quantity`.
   - If a document quotes unit prices per pack (e.g., per 100 or per 1000 units), normalize `unit_price` to the per-unit net price so `quantity * unit_price` equals the printed line net extension.
   - If a document prints tax-inclusive unit prices, derive the net `unit_price` from the printed rate, ONLY when the printed line extensions already include tax. Never strip tax from already tax-exclusive invoices (e.g., invoices with a post-tax credit/discount).

2. FAITHFUL TAX PLACEMENT & SINGLE-LEVEL INVARIANT:
   - A tax charged at the line level (or multi-rate across lines) belongs on `line_items[].taxes[]`.
   - A single tax stated once at the document header belongs in header `taxes[]`.
   - Never populate the SAME tax at both line level and header level (erp.py sums both independently).
   - However, genuinely distinct additive duties/taxes MUST all be preserved.
   - Compound levies (e.g., Ghana NHIL 2.5%, GETFund 2.5%, COVID 1% + 15% VAT on the subtotal+levies) MUST include their explicit printed `tax_amount` so erp.py uses the printed amount rather than recomputing 15% on the un-levied base.
   - Withholding taxes (WHT) that reduce what is owed MUST have a negative `tax_amount` (e.g., "-235.44").

3. DOCUMENT CLASSIFICATION & MULTI-DOCUMENT PACKETS:
   - A single PDF may contain 0, 1, or multiple bookable payables, alongside non-payable supporting pages.
   - Non-payables (Delivery Notes, Proforma Invoices, Dunning/Mahnung reminders, Customs Permits, Air Waybills, Vendor Forms) MUST go in `declined[]` (`{"doc_type": "...", "reason": "..."}`), NEVER in `payables[]`.
   - If an invoice has attached customs/FX breakdown sheets restating the same goods in another currency (e.g., TRY customs sheets attached to a EUR invoice), or a duplicate "Copy" page of the same invoice, emit ONLY the single primary payable.
   - Credit Memos (`invoice_type: "CREDIT_MEMO"`) use the exact same schema with POSITIVE magnitudes for all quantities, prices, taxes, and totals. Normal invoices that merely contain a negative discount line remain `invoice_type: "INVOICE"`.

4. GROUNDING & HONESTY ABOUT THE UNKNOWABLE (RULE 1, 2, 3):
   - Every monetary value must trace to the document. NEVER invent a plug to force gross_total to foot.
   - If a document contains an unprinted embedded excise duty (e.g., embedded IEC where per-line duty is not stated on the page), output the exact printed numbers and set `"_unreconcilable_reason"` explaining why it cannot be deterministically reconciled without inventing figures.
   - Leave master-data codes (`supplier_id`, `po_id`, `tax_type_code`, `payment_term_id`) as `""` when there is no genuine match in master_data.

5. EXACT OUTPUT CONTRACT JSON SCHEMA:
   Return valid JSON with this exact structure:
   {
     "file": "<doc_id>.pdf",
     "payables": [
       {
         "invoice_number": "<printed invoice number>",
         "invoice_type": "INVOICE" or "CREDIT_MEMO",
         "invoice_date": "YYYY-MM-DD",
         "due_date": "YYYY-MM-DD",
         "currency": "<ISO 3-letter currency code>",
         "supplier_name": "<vendor name>",
         "supplier_tax_id": "<vendor VAT / Tax ID>",
         "supplier_address": "<vendor address>",
         "customer_name": "<buyer/customer name>",
         "customer_tax_id": "<buyer tax ID>",
         "customer_address": "<buyer address>",
         "po_number": "<purchase order reference or empty string>",
         "gross_total": "<total amount payable / total gross as dot-decimal>",
         "subtotal": "<net goods total before header taxes as dot-decimal>",
         "total_tax_amount": "<total tax/VAT as dot-decimal>",
         "discount_amount": "<header discount as dot-decimal or 0.00>",
         "freight_charges": "<freight charges as dot-decimal or 0.00>",
         "insurance_charges": "<insurance as dot-decimal or 0.00>",
         "extra_charges": "<extra charges as dot-decimal or 0.00>",
         "excise_duties": "<excise duty as dot-decimal or 0.00>",
         "line_items": [
           {
             "line_number": 1,
             "description": "<item description>",
             "item_code": "<article/product code>",
             "quantity": "<quantity as dot-decimal>",
             "uom": "<unit of measure e.g. PC, EA, KG>",
             "unit_price": "<net unit price per 1 unit as dot-decimal>",
             "discount": "<line discount amount or 0.00>",
             "discount_percentage": "<line discount percent or 0.00>",
             "total": "<line total net extension as dot-decimal>",
             "taxes": [
               {
                 "tax_name": "<VAT / Tax name>",
                 "tax_rate": "<tax rate number without percent>",
                 "tax_amount": "<tax amount for this line or 0.00>"
               }
             ]
           }
         ],
         "taxes": [
           {
             "tax_name": "<VAT / Tax name>",
             "tax_rate": "<tax rate number without percent>",
             "tax_amount": "<header tax amount>"
           }
         ]
       }
     ],
     "declined": [
       {
         "doc_type": "<e.g. Delivery Note, Air Waybill, Customs Permit, Proforma Invoice, Dunning Letter>",
         "reason": "<explanation why this is a non-payable document>"
       }
     ]
   }
"""


def build_initial_user_message(doc_id: str, extracted_text: str, master_summary: str) -> str:
    """Assemble Turn 1 user message with raw evidence in the middle and directive at the bottom."""
    return (
        f"### MIDDLE CONTEXT: REFERENCE MASTER DATA SUMMARY\n{master_summary}\n\n"
        f"### MIDDLE CONTEXT: DOCUMENT EVIDENCE ({doc_id}.pdf)\n{extracted_text}\n\n"
        f"### IMMEDIATE TASK (BOTTOM-OF-CONTEXT ANCHOR)\n"
        f"Analyze `{doc_id}.pdf`, classify all pages/documents inside it, and return a JSON object "
        f"matching the Output Contract (`file`, `payables`, `declined`). Ensure every payable's "
        f"raw components accurately reflect the document structure for `erp_book()`."
    )


def build_repair_turn_message(signals: List[Signal], erp_results: List[dict[str, Any]]) -> str:
    """Append a concise, bounded repair turn to the flowing conversation context.
    
    Places the active diagnostic Signals at the very bottom (recency extreme) so the
    model attends directly to the mathematical/structural facts without re-sending the prompt prefix.
    """
    sig_json = json.dumps([s.to_dict() for s in signals], indent=2)
    erp_json = json.dumps(erp_results, indent=2)
    return (
        f"### ORACLE RECOMPUTE FEEDBACK\n"
        f"Current `erp_book()` output per payable:\n{erp_json}\n\n"
        f"### ACTIVE DIAGNOSTIC SIGNALS (FACTS, NOT INSTRUCTIONS — BOTTOM ANCHOR)\n"
        f"{sig_json}\n\n"
        f"### REPAIR DIRECTIVE\n"
        f"Review the signals above against the document evidence already in context. "
        f"If a signal reflects a presentation-vs-ERP translation issue (e.g., pack scaling, "
        f"tax-inclusive unit price, duplicate line+header tax, withholding tax sign, or a charge "
        f"line item belonging in `extra_charges`/`freight_charges`), repair the payable JSON. "
        f"If every printed number is already faithfully represented and the tension comes from "
        f"an unprinted embedded duty (G8), DO NOT invent or plug any figure — keep the printed "
        f"numbers and set `\"_unreconcilable_reason\"` on the payable."
    )