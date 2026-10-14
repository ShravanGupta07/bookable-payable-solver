"""solver/master_index.py — Scalable O(1) & Token-Blocked Master Data Resolver.

Designed for tenants with 100k–1M+ master records: avoids O(N) full scans per
lookup by building inverted hash indexes on high-cardinality identifiers
(VAT ID, IBAN, Email, PO Number, Country+Rate) and token-blocking indexes
for lexical entity resolution. Follow Rule 2: return "" on genuine no-match.
"""
from __future__ import annotations
import json
import os
import re
from collections import defaultdict
from datetime import date
from typing import Any


def _clean_alnum(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _tokenize(s: str) -> set[str]:
    stop = {"LTD", "LIMITED", "GMBH", "OU", "SDN", "BHD", "PTY", "PLC", "INC", "LDA", "CO", "THE", "AND"}
    tokens = set(re.findall(r"[A-Z0-9]{2,}", (s or "").upper()))
    return tokens - stop or tokens


class MasterDataIndex:
    """Pre-indexed master data store supporting O(1) key lookups and blocked token matching."""

    def __init__(self, master_dir: str) -> None:
        self.master_dir = master_dir
        self.suppliers_by_id: dict[str, dict] = {}
        self.vat_index: dict[str, str] = {}
        self.iban_index: dict[str, str] = {}
        self.email_index: dict[str, str] = {}
        self.name_exact_index: dict[str, str] = {}
        self.supplier_token_blocks: dict[str, set[str]] = defaultdict(set)

        self.po_by_number: dict[str, dict] = {}
        self.tax_by_country_rate: dict[tuple[str, float], list[dict]] = defaultdict(list)
        self.tax_by_rate: dict[float, list[dict]] = defaultdict(list)
        self.payment_terms: list[dict] = []
        self.payment_alias_index: dict[str, str] = {}
        self.payment_days_index: dict[int, str] = {}
        self.business_units: list[dict] = []

        self._load_and_index()

    def _load_json(self, filename: str) -> dict[str, Any]:
        path = os.path.join(self.master_dir, filename)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _load_and_index(self) -> None:
        # 1. Index Suppliers (O(1) VAT/IBAN/Email/ExactName + Token Blocking)
        for s in self._load_json("suppliers.json").get("suppliers", []):
            sid = s["supplier_id"]
            self.suppliers_by_id[sid] = s
            if s.get("vat_id"):
                self.vat_index[_clean_alnum(s["vat_id"])] = sid
                # Also index stripped of country prefix if present
                raw_digits = re.sub(r"^[A-Z\-]+", "", s["vat_id"].upper()).replace("-", "")
                if len(raw_digits) >= 6:
                    self.vat_index[_clean_alnum(raw_digits)] = sid
            if s.get("bank_iban"):
                self.iban_index[_clean_alnum(s["bank_iban"])] = sid
            if s.get("email"):
                self.email_index[s["email"].strip().lower()] = sid
            norm_name = _clean_alnum(s.get("name", ""))
            if norm_name:
                self.name_exact_index[norm_name] = sid
            for tok in _tokenize(s.get("name", "")):
                self.supplier_token_blocks[tok].add(sid)

        # 2. Index POs (O(1) by normalized po_number)
        for po in self._load_json("po_master.json").get("purchase_orders", []):
            self.po_by_number[_clean_alnum(po["po_number"])] = po

        # 3. Index Taxes (O(1) by (country, rate) and rate)
        for t in self._load_json("tax_master.json").get("taxes", []):
            rate = round(float(t["rate"]), 3)
            country = (t.get("country") or "").upper()
            self.tax_by_country_rate[(country, rate)].append(t)
            self.tax_by_rate[rate].append(t)

        # 4. Index Payment Terms
        for pt in self._load_json("payment_terms.json").get("payment_terms", []):
            self.payment_terms.append(pt)
            ptid = pt["payment_term_id"]
            for alias in pt.get("text_aliases", []):
                self.payment_alias_index[alias.strip().lower()] = ptid
            if pt.get("days") is not None and pt["days"] > 0:
                self.payment_days_index[int(pt["days"])] = ptid

        # 5. Index Chart of Books (Buyer Org)
        for comp in self._load_json("chart_of_books.json").get("companies", []):
            c_code = comp["company_code"]
            for bu in comp.get("business_units", []):
                for loc in bu.get("locations", []):
                    self.business_units.append({
                        "company_code": c_code,
                        "business_unit_code": bu["business_unit_code"],
                        "business_unit_name": bu["business_unit_name"],
                        "location_code": loc["location_code"],
                        "location_name": loc["location_name"],
                        "address": loc["invoice_to_address"],
                        "tokens": _tokenize(bu["business_unit_name"] + " " + loc["invoice_to_address"]),
                    })

    def resolve_supplier(self, name: str = "", vat_id: str = "", iban: str = "", email: str = "") -> str:
        """Resolve supplier_id using strict deterministic hierarchy; return '' if no real match."""
        if vat_id:
            cv = _clean_alnum(vat_id)
            if cv in self.vat_index:
                return self.vat_index[cv]
            stripped = re.sub(r"^[A-Z]+", "", cv)
            if len(stripped) >= 6 and stripped in self.vat_index:
                return self.vat_index[stripped]
        if iban:
            ci = _clean_alnum(iban)
            if ci in self.iban_index:
                return self.iban_index[ci]
        if email and email.strip().lower() in self.email_index:
            return self.email_index[email.strip().lower()]

        norm_name = _clean_alnum(name)
        if not norm_name:
            return ""
        if norm_name in self.name_exact_index:
            return self.name_exact_index[norm_name]

        # Blocked token Jaccard similarity (avoids full table scan)
        q_tokens = _tokenize(name)
        if not q_tokens:
            return ""
        candidate_ids: set[str] = set()
        for tok in q_tokens:
            candidate_ids |= self.supplier_token_blocks.get(tok, set())

        best_id, best_score = "", 0.0
        for sid in candidate_ids:
            s_tokens = _tokenize(self.suppliers_by_id[sid]["name"])
            if not s_tokens:
                continue
            inter = len(q_tokens & s_tokens)
            union = len(q_tokens | s_tokens)
            score = inter / union
            # Require high token overlap (>= 0.65) to prevent false-positive fabrication
            if score > best_score and score >= 0.65:
                best_score, best_id = score, sid
        return best_id

    def resolve_po(self, po_number: str) -> str:
        if not po_number:
            return ""
        key = _clean_alnum(po_number)
        hit = self.po_by_number.get(key)
        return hit["po_id"] if hit else ""

    def resolve_buyer(self, buyer_name: str = "", buyer_address: str = "", currency: str = "") -> dict[str, str]:
        q_text = f"{buyer_name} {buyer_address}".upper()
        q_tokens = _tokenize(q_text)
        best_bu = None
        best_overlap = 0
        for bu in self.business_units:
            if bu["business_unit_name"].upper() in q_text:
                return {
                    "company_code": bu["company_code"],
                    "business_unit_code": bu["business_unit_code"],
                    "location_code": bu["location_code"],
                }
            overlap = len(q_tokens & bu["tokens"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_bu = bu
        if best_bu and best_overlap >= 2:
            return {
                "company_code": best_bu["company_code"],
                "business_unit_code": best_bu["business_unit_code"],
                "location_code": best_bu["location_code"],
            }
        return {"company_code": "", "business_unit_code": "", "location_code": ""}

    def resolve_payment_term(self, raw_term: str = "", invoice_date: str = "", due_date: str = "") -> str:
        low = (raw_term or "").strip().lower()
        if low in self.payment_alias_index:
            return self.payment_alias_index[low]
        for alias, ptid in self.payment_alias_index.items():
            if alias and alias in low:
                return ptid
        # Derive from (due_date - invoice_date) if both are valid ISO dates
        if invoice_date and due_date:
            try:
                d0 = date.fromisoformat(invoice_date[:10])
                d1 = date.fromisoformat(due_date[:10])
                delta_days = (d1 - d0).days
                if delta_days == 0 and ("immediate" in low or "receipt" in low):
                    return "Immediate"
                if delta_days in self.payment_days_index:
                    return self.payment_days_index[delta_days]
            except ValueError:
                pass
        return ""

    def resolve_tax_code(self, rate: float, country: str = "", tax_name: str = "", is_withholding: bool = False) -> str:
        r = round(abs(float(rate)), 3)
        c = (country or "").upper().strip()
        name_up = (tax_name or "").upper()

        candidates = self.tax_by_country_rate.get((c, r), []) if c else []
        if not candidates:
            candidates = self.tax_by_rate.get(r, [])
            if c:
                # If country was known and had no match at that rate, do not cross-contaminate countries
                return ""
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0]["code"]

        # Disambiguate multiple taxes at same rate (e.g. GH 2.5% NHIL vs GETFL, or DE 0% RC)
        for cand in candidates:
            tt = cand["tax_type"].upper()
            cname = cand["name"].upper()
            if is_withholding and tt == "WHT":
                return cand["code"]
            if tt in name_up or any(w in name_up for w in cname.split() if len(w) > 3):
                return cand["code"]
        return candidates[0]["code"]