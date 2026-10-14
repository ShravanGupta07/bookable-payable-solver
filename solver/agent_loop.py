"""solver/agent_loop.py — Flowing-Context Reconcile-Repair Solver Loop.

Implements B0 & B1 Solver Engineering:
- Flowing, bounded conversation state per document (single coherent context).
- Cache-friendly static system prefix (`STATIC_SYSTEM_PREFIX`) + variable tail.
- Tiered intelligence routing: fast extraction model on Turn 1; escalates to
  high-reasoning model ONLY when diagnostic signals or ERP reconcile deltas fire.
- Strict convergence & anti-spin termination.
"""
from __future__ import annotations
import json
from typing import Any, Callable, List
import erp
from solver.deterministic import apply_deterministic_invariants
from solver.master_index import MasterDataIndex
from solver.prompt_cache import (
    STATIC_SYSTEM_PREFIX,
    build_initial_user_message,
    build_repair_turn_message,
)
from solver.signals import Signal, diagnose_payable

MAX_REPAIR_TURNS = 3


def is_converged(doc_output: dict[str, Any]) -> tuple[bool, List[Signal]]:
    """Check whether all payables in the document output have converged with erp_book()."""
    payables = doc_output.get("payables") or []
    all_signals: List[Signal] = []
    for i, p in enumerate(payables):
        # If the clerk explicitly flagged G8 (deterministically unreconcilable embedded duty),
        # respect the flag-not-fake boundary and do not force a fake repair loop.
        if p.get("_unreconcilable_reason") or p.get("deterministically_reconcilable") is False:
            continue
        sigs = diagnose_payable(p, idx=i)
        all_signals.extend(sigs)
    return (len(all_signals) == 0), all_signals


def solve_document(
    doc_id: str,
    extracted_text: str,
    master_idx: MasterDataIndex,
    llm_call: Callable[[List[dict[str, str]], str], dict[str, Any]],
    fast_model: str = "gemini-2.5-flash",
    strong_model: str = "gemini-2.5-pro",
) -> dict[str, Any]:
    """Run the multi-turn agentic solver loop for a single PDF document."""
    clean_companies = [
        {k: v for k, v in bu.items() if k != "tokens"}
        for bu in master_idx.business_units
    ]
    master_summary = json.dumps({
        "suppliers_sample": list(master_idx.suppliers_by_id.values()),
        "taxes": [t for ts in master_idx.tax_by_rate.values() for t in ts],
        "companies": clean_companies,
        "payment_terms": master_idx.payment_terms,
        "purchase_orders": list(master_idx.po_by_number.values()),
    }, indent=2)

    # Flowing conversation context: Message[0] is the static cacheable system prefix
    messages: List[dict[str, str]] = [
        {"role": "system", "content": STATIC_SYSTEM_PREFIX},
        {"role": "user", "content": build_initial_user_message(doc_id, extracted_text, master_summary)},
    ]

    # Turn 1: Fast model initial extraction
    state = llm_call(messages, fast_model)
    state["file"] = f"{doc_id}.pdf"
    state["payables"] = [
        apply_deterministic_invariants(p, master_idx)
        for p in (state.get("payables") or [])
    ]

    converged, signals = is_converged(state)
    prev_signature = json.dumps(state.get("payables", []), sort_keys=True)

    # Turn 2..MAX_REPAIR_TURNS: Escalate to strong_model only if signals fired
    turn = 1
    while not converged and turn < MAX_REPAIR_TURNS:
        turn += 1
        erp_results = [erp.erp_book(p) for p in (state.get("payables") or [])]

        # Append assistant's previous draft and the bottom-anchored Signal feedback
        messages.append({"role": "assistant", "content": json.dumps(state)})
        messages.append({"role": "user", "content": build_repair_turn_message(signals, erp_results)})

        # Bound context window: keep system [0], initial doc [1], and latest repair pair [-2:]
        if len(messages) > 6:
            messages = messages[:2] + messages[-4:]

        state = llm_call(messages, strong_model)
        state["file"] = f"{doc_id}.pdf"
        state["payables"] = [
            apply_deterministic_invariants(p, master_idx)
            for p in (state.get("payables") or [])
        ]

        converged, signals = is_converged(state)
        curr_signature = json.dumps(state.get("payables", []), sort_keys=True)
        if curr_signature == prev_signature:
            # Anti-spin termination: state did not evolve
            break
        prev_signature = curr_signature

    return state