"""Turn one company record into checked facts and red flags.

This file uses no LLM. Every fact here is computed by plain Python, so we can trust it.
The LLM (in summarise.py) only turns these facts into sentences.
The evaluation (in evaluate.py) uses the same red flags as a checklist.
"""

from typing import Any

# How serious each kind of red flag is. A lower number is more serious.
SEVERITY = {
    "winding_up_status": 1,
    "winding_up_petition": 1,
    "audit_warning": 2,
    "negative_equity": 2,
    "pending_defendant_case": 3,
    "loss": 4,
    "revenue_drop": 4,
    "missing_financials": 5,
}


def rm(amount: float) -> str:
    """Format a ringgit amount the short way, for example 1250000 -> 'RM1.25m'."""
    if abs(amount) >= 1_000_000:
        return f"RM{amount / 1_000_000:.2f}m"
    if abs(amount) >= 1_000:
        return f"RM{amount / 1_000:.0f}k"
    return f"RM{amount:.0f}"


def red_flags(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the red flags in one record, most serious first.

    Each flag is a dict: {"kind": ..., "detail": "a sentence a human can read"}.
    """
    flags: list[dict[str, Any]] = []
    profile = record["profile"]
    years = sorted(record["financials"]["years"], key=lambda y: y["financialYear"])
    cases = record["litigation"]["cases"]

    # 1. The company itself is being closed down.
    if profile["status"] != "Active":
        flags.append({"kind": "winding_up_status", "detail": f"Company status is '{profile['status']}'."})

    # 2. Someone has asked a court to close the company down.
    for case in cases:
        if case["caseType"] == "Winding-up petition" and case["role"] == "defendant":
            flags.append(
                {
                    "kind": "winding_up_petition",
                    "detail": f"Winding-up petition against the company, filed {case['filedOn']}, status {case['status']}.",
                }
            )

    # 3. The auditor did not give a clean opinion.
    for year in years:
        opinion = year["audit"]["opinion"]
        if opinion != "Unqualified":
            flags.append({"kind": "audit_warning", "detail": f"FY{year['financialYear']} audit opinion: {opinion}."})

    # 4. Court cases the company may still have to pay for (it is the defendant, and the case is not over).
    #    Winding-up petitions are already covered above, so we skip them here.
    pending = [
        c for c in cases if c["role"] == "defendant" and c["status"] == "Pending" and c["caseType"] != "Winding-up petition"
    ]
    if pending:
        total = sum(c["claimAmount"] or 0 for c in pending)
        flags.append(
            {
                "kind": "pending_defendant_case",
                "detail": f"{len(pending)} pending case(s) against the company, claims totalling {rm(total)}.",
            }
        )

    # 5. Financial health in the latest year we have.
    if years:
        latest = years[-1]["statements"]
        fy = years[-1]["financialYear"]
        if latest["totalEquity"] < 0:
            flags.append(
                {
                    "kind": "negative_equity",
                    "detail": f"Negative equity of {rm(-latest['totalEquity'])} in FY{fy} (owes more than it owns).",
                }
            )
        if latest["profitAfterTax"] < 0:
            flags.append({"kind": "loss", "detail": f"Loss after tax of {rm(-latest['profitAfterTax'])} in FY{fy}."})
        first = years[0]["statements"]
        if len(years) > 1 and first["revenue"] and latest["revenue"] < 0.7 * first["revenue"]:
            drop = 1 - latest["revenue"] / first["revenue"]
            flags.append(
                {"kind": "revenue_drop", "detail": f"Revenue fell {drop:.0%} from FY{years[0]['financialYear']} to FY{fy}."}
            )

    # 6. Missing data. The reader must know what we could not check.
    if not years:
        flags.append({"kind": "missing_financials", "detail": "No financial statements on record."})

    return sorted(flags, key=lambda f: SEVERITY[f["kind"]])


def key_facts(record: dict[str, Any]) -> dict[str, Any]:
    """The context a reader needs besides the red flags: what, how big, how old, what trend."""
    profile = record["profile"]
    years = sorted(record["financials"]["years"], key=lambda y: y["financialYear"])
    cases = record["litigation"]["cases"]
    facts: dict[str, Any] = {
        "name": profile["companyName"],
        "registrationNumber": record["registrationNumber"],
        "business": profile["natureOfBusiness"],
        "industry": profile["industry"],
        "state": profile["state"],
        "incorporated": profile["incorporationDate"],
        "status": profile["status"],
        "directors": len(record["relationships"]["directors"]),
        "corporateShareholders": [s["name"] for s in record["relationships"]["shareholders"] if s["type"] != "individual"],
        "formerNames": [f["name"] for f in profile["formerNames"]],
        "financialYearsOnRecord": [y["financialYear"] for y in years],
        "courtCases": {
            "total": len(cases),
            "asPlaintiff": sum(1 for c in cases if c["role"] == "plaintiff"),
            "asDefendant": sum(1 for c in cases if c["role"] == "defendant"),
        },
    }
    if years:
        latest = years[-1]
        s = latest["statements"]
        facts["latestYear"] = {
            "financialYear": latest["financialYear"],
            "revenue": rm(s["revenue"]),
            "profitAfterTax": rm(s["profitAfterTax"]),
            "totalEquity": rm(s["totalEquity"]),
            "borrowings": rm(s["borrowings"]),
            "netProfitMargin": latest["ratiosAndMetrics"]["profitability"]["netProfitMargin"],
        }
        facts["revenueByYear"] = {y["financialYear"]: rm(y["statements"]["revenue"]) for y in years}
    return facts
