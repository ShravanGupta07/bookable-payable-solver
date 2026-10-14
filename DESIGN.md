# DESIGN.md — The Bookable Payable Solver

## 1. What we understood about these documents that was not obvious on day one

On day one, the problem looks like document extraction: read the PDF, fill the JSON schema, and match master-data IDs. That approach stalls around a third of the corpus (`DU-05`, `DU-10`, `INV-06`, `INV-19`, `HLD-01`, `HLD-03`, `HLD-05`), and at first the failures look like a dozen unrelated edge cases.

The shift came from reading `erp.py` not as a validator, but as a **rigid mathematical model** that often disagrees with how suppliers format pages. **A faithful copy of the page is frequently a wrong answer because document presentation $\neq$ the ERP's data model:**

- **Net vs. Inclusive Basis (`INC` vs `INC-twin`):** `erp.py` assumes `unit_price` is strictly net of tax and adds `_line_taxes()` and `_header_taxes()` on top. When a supplier prints tax-inclusive unit prices (`INV-06`, `HLD-03`), copying the printed unit price causes the ERP to double-tax the line. Conversely, an invoice with tax-exclusive prices and a post-tax credit (`INV-04`, `INV-07`) looks superficially similar if you only compare totals, so stripping tax blindly corrupts valid control twins.
- **Pricing Unit Scaling (`SCALE`):** `erp.py` multiplies `quantity * unit_price` assuming price per 1 unit. When a supplier quotes a price per pack of 100 or 1,000 (`DU-05` with `PU=100`, `INV-26`, `INV-27`, `INV-28`), a literal copy inflates the ERP line base by $100\times$ or $1000\times$.
- **Independent Tax Summation (`DUP` vs `DUP-twin`):** `erp.py` adds `_line_taxes` and `_header_taxes` independently. When a document prints VAT per line and also summarizes that same VAT at the footer (`DU-10`, `DU-02`), populating both levels doubles the tax. Yet when a document carries genuinely distinct additive duties (`INV-09`), both must be kept.
- **Compound Tax Bases & Directed Taxes (`CMP`, `TAXDIR`):** In `erp.py`, if `tax_amount` is empty, header tax is derived as `round2(net_base * rate / 100)`. On compound-levy invoices (`INV-19` in Ghana, where 15% VAT is levied on `net + NHIL + GETFund + COVID`), leaving `tax_amount` blank under-computes the VAT; supplying the explicit printed `tax_amount` lets `erp.py` book the exact statutory figure. Similarly, withholding taxes (`HLD-01`, 3% WHT) reduce what is owed and must carry a negative `tax_amount`.
- **Packet Granularity (`MULTI`, `MULTI-foil`, `DECLINE`):** A PDF file is a mailbag, not an invoice. It may hold zero payables (`DU-05s` delivery note, `DU-08` *Mahnung*/dunning notice, `DU-09` form, `INV-23` proforma), duplicate copies of one invoice (`INV-25`), a single EUR invoice accompanied by 18 foreign-currency (`TRY`) customs restatement sheets (`DU-02`), or multiple distinct payables mixed with waybills and customs permits (`DU-03`).

---

## 2. What our system does when it meets an unseen document — and why it generalises

Rather than building a brittle rulebook of per-supplier `if/else` branches, we engineered an **agentic Reconcile-Repair Solver (`solver/agent_loop.py`)** separated by a strict **Determinism Boundary at Ambiguity (`solver/deterministic.py`, `solver/signals.py`)**:

1. **Deterministic Foundation (`solver/deterministic.py` & `solver/master_index.py`):**
   Everything math-certain or provable happens in deterministic Python:
   - Locale number normalization into dot-decimal strings (`normalize_dot_decimal`).
   - Credit memo sign normalization (`CREDIT_MEMO` values normalized to positive magnitudes while preserving normal invoices with negative discount lines like `INV-13`).
   - Exact pack-scaling normalization when `qty * unit_price / printed_line_total` is provably $10\times, 100\times,$ or $1000\times$.
   - Scalable $O(1)$ hash-indexed and token-blocked master-data resolution (`MasterDataIndex`), returning `""` on genuine no-matches (`HLD-08` supplier, non-ERP PO references).

2. **Signals, Not Instructions (`solver/signals.py`):**
   After Turn-1 extraction, we pass each payable through `erp.erp_book(payable)` and `diagnose_payable()`. Instead of telling the model *how* to fix a mismatch or leaking the target gross as a plug, the system emits structured `Signal(field_path, kind, code, message)` objects stating **accounting and ERP facts**:
   - `TAX_AT_BOTH_LEVELS`: states that `erp.py` sums `_line_taxes` and `_header_taxes` independently, asking the model to verify whether the document shows one restated tax or distinct additive levies.
   - `DELTA_EQUALS_TOTAL_TAX`: states that the recompute delta equals the printed tax total—characteristic of tax-inclusive unit prices or duplicated tax levels.
   - `RECONCILE_COMPONENT_TENSION`: a target-less component-to-header tension signal prompting the clerk to re-verify withholding tax signs, charge buckets (`extra_charges` / `freight_charges`), compound tax amounts, or embedded duties.

3. **Flowing Context, Positional Leverage, Prompt Caching & Tiered Routing (`solver/prompt_cache.py`, `solver/agent_loop.py`):**
   - **Cache-Friendly Static Prefix:** `STATIC_SYSTEM_PREFIX` (`solver/prompt_cache.py`) is 100% invariant across all documents and turns, maximizing KV-cache hits.
   - **Lost-in-the-Middle Layout:** Core ERP invariants sit at the very top of the prompt, raw OCR/master evidence sits in the middle, and active `Signal` objects sit at the bottom recency extreme.
   - **Tiered Cost Economics:** Turn 1 uses a fast model (`fast_model`). Documents that immediately reconcile with zero signals terminate on Turn 1. Only documents that emit diagnostic signals escalate to the high-reasoning model (`strong_model`) in a bounded, flowing multi-turn conversation (`MAX_REPAIR_TURNS = 3`) with anti-spin termination.

---

## 3. Which document could not be solved the way the others were — and how we knew

**`HLD-05.pdf` (`Fatura_25252033008246990401003957.PDF`, Distribeer Bebidas Lda, `1,037.94 EUR`)** cannot be deterministically reconciled from the figures printed on the page without violating Rule 1 (inventing unprinted numbers).

### How we knew:
1. Every line item's printed quantity, unit price, discount percentage, and net line total on `HLD-05.pdf` are clearly legible and internally consistent.
2. However, the invoice is a Portuguese beverage invoice subject to **IEC (*Imposto Especial sobre o Consumo* — alcohol excise duty)**. The supplier embeds the IEC excise duty into the VAT taxable base (producing header IVA of `157.05 EUR` at 23% and `19.82 EUR` at 13% and a gross total of `1,037.94 EUR`), **without printing the per-line excise duty amounts on the document**.
3. When `erp.py` recomputes the gross from the printed line quantities, net unit prices, discounts, and printed IVA amounts, the result falls short of `1,037.94 EUR` by the exact amount of the unprinted embedded IEC duty.
4. Any attempt to make `erp_book()` return `1037.94` would require back-solving and injecting an unprinted `excise_duties` figure (a plug). Instead, our solver verifies that all printed fields are grounded, refuses to fabricate an unprinted number, and flags the payable with `_unreconcilable_reason` (`deterministically_reconcilable: false`).