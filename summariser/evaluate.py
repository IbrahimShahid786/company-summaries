"""Score the summaries automatically, so we can compare versions with numbers instead of opinions.

Usage:
    uv run python -m summariser.evaluate v1 v2

Four checks per summary:
1. Flag recall   - of the red flags facts.py found, how many does the summary mention?
2. Verdict right - does line 1 say "no red flags" exactly when there are none, and give a real count otherwise?
3. Numbers right - does every RM amount and % in the summary match a number in the record?
4. Length        - word count (the target is 120 or less).

These checks use simple keyword matching, so they are not perfect. We also read a sample by hand (see CASE_STUDY.md).
"""

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

from summariser.facts import red_flags

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "data" / "companies.jsonl"
SUMMARIES = ROOT / "summaries"
RESULTS = ROOT / "results"

# Words that show a summary mentioned each kind of red flag.
MENTIONS = {
    "winding_up_status": [r"winding[ -]up"],
    "winding_up_petition": [r"petition"],
    "audit_warning": [r"disclaimer", r"going concern", r"audit"],
    "negative_equity": [r"negative equity", r"liabilities exceed", r"owes more"],
    "pending_defendant_case": [r"pending"],
    "loss": [r"\bloss"],
    "revenue_drop": [r"revenue (fell|dropped|declined|decreased)", r"(fall|drop|decline|decrease) in revenue"],
    "missing_financials": [
        r"no financ",
        r"financials? (are |is )?(missing|not available|unavailable)",
        r"no (audited )?accounts",
    ],
}


def all_numbers(value: Any) -> list[float]:
    """Every number anywhere in the record (amounts and ratios), found by walking the JSON."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in all_numbers(v)]
    if isinstance(value, list):
        return [n for v in value for n in all_numbers(v)]
    return []


def numbers_in_summary(text: str) -> list[tuple[str, float, float]]:
    """Find RM amounts and percentages in the summary, as (what was written, value, rounding allowed).

    The rounding allowed depends on how the number was written: "RM4k" can be anything from RM3,500 to RM4,500.
    """
    found = []
    for m in re.finditer(r"RM\s?([\d,]+(?:\.(\d+))?)\s?(million|mil|m|k|thousand)?\b", text, re.IGNORECASE):
        unit = (m.group(3) or "").lower()
        scale = 1_000_000 if unit in ("million", "mil", "m") else 1_000 if unit in ("k", "thousand") else 1
        decimals = len(m.group(2) or "")
        found.append((m.group(0), float(m.group(1).replace(",", "")) * scale, 0.5 * scale / 10**decimals))
    for m in re.finditer(r"(-?\d+(?:\.(\d+))?)\s?%", text):
        decimals = len(m.group(2) or "")
        found.append((m.group(0), float(m.group(1)) / 100, 0.5 / 100 / 10**decimals))
    return found


def number_is_right(value: float, rounding: float, record_numbers: list[float]) -> bool:
    """True if the value matches a record number, allowing for the rounding the writer used (or 1%)."""
    for n in record_numbers:
        # "a loss of RM1.2m" is written positive but stored negative; shareholdings are stored as 58.25, not 0.5825
        for candidate in (n, -n, n / 100):
            if abs(value - candidate) <= max(abs(candidate) * 0.01, rounding):
                return True
    return False


def score(record: dict[str, Any], summary: str) -> dict[str, Any]:
    flags = red_flags(record)
    text = summary.lower()
    kinds = sorted({f["kind"] for f in flags})
    missed = [k for k in kinds if not any(re.search(p, text) for p in MENTIONS[k])]

    first_line = summary.strip().splitlines()[0].lower() if summary.strip() else ""
    says_clean = "no red flag" in first_line
    # Right = says "no red flags" exactly when there are none, and otherwise gives a real number (not "N issue(s)").
    gives_number = bool(re.search(r"\b(\d+|one|two|three|four|five|six|seven)\b", first_line))
    verdict_right = (not flags) if says_clean else (bool(flags) and gives_number)

    record_numbers = all_numbers(record)
    # Also allow revenue changes that facts.py computes (e.g. "fell 35%").
    years = sorted(record["financials"]["years"], key=lambda y: y["financialYear"])
    if len(years) > 1 and years[0]["statements"]["revenue"]:
        change = years[-1]["statements"]["revenue"] / years[0]["statements"]["revenue"] - 1
        record_numbers += [change, abs(change)]
    # And totals of court claims (e.g. "2 pending cases, RM779k in claims").
    cases = record["litigation"]["cases"]
    record_numbers += [
        sum(c["claimAmount"] or 0 for c in cases),
        sum(c["claimAmount"] or 0 for c in cases if c["role"] == "defendant"),
        sum(c["claimAmount"] or 0 for c in cases if c["role"] == "defendant" and c["status"] == "Pending"),
        sum(
            c["claimAmount"] or 0
            for c in cases
            if c["role"] == "defendant" and c["status"] == "Pending" and c["caseType"] != "Winding-up petition"
        ),
    ]
    numbers = numbers_in_summary(summary)
    wrong = [written for written, value, rounding in numbers if not number_is_right(value, rounding, record_numbers)]

    return {
        "registrationNumber": record["registrationNumber"],
        "flags": len(kinds),
        "missed_flags": ";".join(missed),
        "flag_recall": (len(kinds) - len(missed)) / len(kinds) if kinds else None,
        "verdict_right": verdict_right,
        "numbers": len(numbers),
        "wrong_numbers": ";".join(wrong),
        "words": len(summary.split()),
    }


def main() -> None:
    versions = sys.argv[1:] or ["v1", "v2", "v3", "v4"]
    records = {
        r["registrationNumber"]: r for r in (json.loads(line) for line in COMPANIES.read_text(encoding="utf-8").splitlines())
    }
    RESULTS.mkdir(exist_ok=True)

    print(f"{'version':<8}{'companies':>10}{'flag recall':>13}{'verdict right':>15}{'wrong numbers':>15}{'avg words':>11}")
    for version in versions:
        path = SUMMARIES / f"{version}.jsonl"
        if not path.exists():
            print(f"{version:<8}  (no summaries yet: run summarise.py {version} first)")
            continue
        rows = [
            score(records[s["registrationNumber"]], s["summary"])
            for s in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
        ]

        with (RESULTS / f"{version}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        total_flags = sum(r["flags"] for r in rows)
        found_flags = total_flags - sum(len(r["missed_flags"].split(";")) for r in rows if r["missed_flags"])
        total_numbers = sum(r["numbers"] for r in rows)
        wrong_numbers = sum(len(r["wrong_numbers"].split(";")) for r in rows if r["wrong_numbers"])
        flag_cell = f"{found_flags}/{total_flags}"
        verdict_cell = f"{sum(r['verdict_right'] for r in rows)}/{len(rows)}"
        number_cell = f"{wrong_numbers}/{total_numbers}"
        avg_words = sum(r["words"] for r in rows) / len(rows)
        print(f"{version:<8}{len(rows):>10}{flag_cell:>13}{verdict_cell:>15}{number_cell:>15}{avg_words:>11.0f}")
    print(f"\nPer-company details are in {RESULTS}/<version>.csv")


if __name__ == "__main__":
    main()
