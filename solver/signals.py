"""solver/signals.py — Fact-based Signal primitives (Signals, not instructions).

States mathematical and structural facts about how a candidate payable interacts
with the sealed ERP model (erp.py), without prescribing hardcoded remedies or
leaking target totals as plugs.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, List
import erp


@dataclass(frozen=True)
class Signal:
    """A structured diagnostic signal emitted at the determinism boundary.
    
    Attributes:
        field_path: JSON path where the anomaly or fact was observed.
        kind: Category of signal ('GROUNDED_FACT' for deterministic invariants,
              'JUDGMENT_SIGNAL' for target-less mathematical/structural tensions).
        code: Machine-readable signal identifier.
        message: Statement of accounting/ERP fact (never a 'how-to' instruction).
    """
    field_path: str
    kind: str          # "GROUNDED_FACT" | "JUDGMENT_SIGNAL"
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def diagnose_payable(payable: dict[str, Any], idx: int = 0) -> List[Signal]:
    """Inspect one payable against ERP invariants and emit factual signals."""
    signals: List[Signal] = []
    prefix = f"payables[{idx}]"
    line_items = payable.get("line_items") or []
    header_taxes = payable.get("taxes") or []

    # 1. Fact: ERP sums line taxes and header taxes independently
    has_line_taxes = any(
        bool(li.get("taxes"))
        or bool(str(li.get("tax_rate") or "").strip())
        or bool(str(li.get("tax_amount") or "").strip())
        for li in line_items
    )
    has_header_taxes = any(
        erp.num(t.get("tax_rate")) != 0 or erp.num(t.get("tax_amount")) != 0
        for t in header_taxes if isinstance(t, dict)
    )
    if has_line_taxes and has_header_taxes:
        signals.append(Signal(
            field_path=f"{prefix}.taxes",
            kind="JUDGMENT_SIGNAL",
            code="TAX_AT_BOTH_LEVELS",
            message=(
                "Fact: Both line_items[].taxes and header taxes[] carry non-zero tax entries. "
                "In erp.py, _line_taxes() and _header_taxes() are summed independently into gross. "
                "Verify on the document whether these are genuinely distinct additive levies/duties "
                "or the same tax restated at line and header summary."
            ),
        ))

    # 2. Fact: Line extension vs (quantity * unit_price - discount)
    sum_printed_line_totals = 0.0
    sum_erp_line_bases = 0.0
    for li_idx, li in enumerate(line_items):
        erp_base = erp._line_base(li)
        sum_erp_line_bases += erp_base
        printed_total = erp.num(li.get("total"))
        sum_printed_line_totals += printed_total

        if printed_total > 0 and erp_base > 0:
            ratio = erp_base / printed_total
            # Pack-unit scaling fact (e.g. price per 10, 100, 1000)
            for scale in (10.0, 100.0, 1000.0):
                if abs(ratio - scale) / scale < 0.015:
                    signals.append(Signal(
                        field_path=f"{prefix}.line_items[{li_idx}].unit_price",
                        kind="GROUNDED_FACT",
                        code="PACK_SCALING_RATIO",
                        message=(
                            f"Fact: ERP line base (qty * unit_price = {erp_base:.2f}) is {scale:g}x "
                            f"the printed line total ({printed_total:.2f}), indicating a price-per-{int(scale)} "
                            f"pricing unit on the document."
                        ),
                    ))
                    break
            else:
                if abs(erp_base - printed_total) > max(0.05, printed_total * 0.005):
                    signals.append(Signal(
                        field_path=f"{prefix}.line_items[{li_idx}]",
                        kind="JUDGMENT_SIGNAL",
                        code="LINE_EXTENSION_MISMATCH",
                        message=(
                            f"Fact: ERP computed line net base {erp_base:.2f} from qty={li.get('quantity')}, "
                            f"unit_price={li.get('unit_price')}, discount={li.get('discount')}, "
                            f"discount_pct={li.get('discount_percentage')}, whereas printed line total is {printed_total:.2f}."
                        ),
                    ))

    # 3. Fact: ERP recomputed gross vs document's stated gross_total (Target-less diagnosis)
    booked = erp.erp_book(payable)["will_book_gross"]
    stated_gross = erp.num(payable.get("gross_total"))
    total_tax_printed = erp.num(payable.get("total_tax_amount"))

    if stated_gross > 0:
        delta = erp.round2(booked - stated_gross)
        tol = max(0.02, abs(stated_gross) * 0.0005)
        if abs(delta) > tol:
            # Check specific structural facts that explain the delta without guessing
            if total_tax_printed > 0 and abs(delta - total_tax_printed) <= 0.15:
                signals.append(Signal(
                    field_path=f"{prefix}.line_items",
                    kind="JUDGMENT_SIGNAL",
                    code="DELTA_EQUALS_TOTAL_TAX",
                    message=(
                        f"Fact: ERP recomputed gross exceeds stated gross by +{delta:.2f}, which matches "
                        f"the document's total tax ({total_tax_printed:.2f}). This occurs either when "
                        f"unit_price on the page is already tax-inclusive (ERP expects NET unit_price) "
                        f"or when the same tax is entered at both line and header levels."
                    ),
                ))
            else:
                signals.append(Signal(
                    field_path=f"{prefix}.gross_total",
                    kind="JUDGMENT_SIGNAL",
                    code="RECONCILE_COMPONENT_TENSION",
                    message=(
                        f"Fact: ERP recompute from current components yields a component-to-header "
                        f"tension of {delta:+.2f} (ERP line bases sum to {sum_erp_line_bases:.2f}). "
                        f"Check whether a printed withholding tax (negative tax_amount), compound tax "
                        f"amount, header discount, freight/extra charge, or unit-scaling factor was "
                        f"misplaced, or if the document contains an unprinted embedded excise duty "
                        f"(in which case, preserve grounded printed figures and flag as unreconcilable)."
                    ),
                ))

    return signals