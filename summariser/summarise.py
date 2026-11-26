"""Write a summary for each company with an LLM, and save it for the viewer.

Usage (from the repo folder):
    uv run python -m summariser.summarise v1            # all 50 companies
    uv run python -m summariser.summarise v2 --limit 5  # only the first 5, for a quick test

v1 = baseline: the LLM reads the raw record.
v2 = Python finds the facts and red flags first (facts.py), the LLM only writes the sentences.
v3 = like v2, but Python also writes the verdict line, because the LLM sometimes wrote "N issue(s)".
v4 = Python writes the verdict and the red-flag bullets; the LLM only writes 1-2 sentences of context,
     because in v3 it sometimes added bullets that contradicted the verdict, and padded the end with filler.

Every LLM response is saved in runs/<version>/, so running again re-uses it instead of calling the API.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq, RateLimitError

from summariser.facts import key_facts, red_flags

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "data" / "companies.jsonl"
SUMMARIES = ROOT / "summaries"
RUNS = ROOT / "runs"

load_dotenv(ROOT / ".env")  # reads GROQ_API_KEY from the .env file
# Free models change often. To switch, set GROQ_MODEL=<name> in .env.
# To list the models your key can use: uv run python -m summariser.summarise --list-models
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# ---------- Prompts ----------

RULES = """You write summaries for a team that vets companies before choosing them as suppliers or partners.
The summary answers one question: is there any reason not to proceed, or to look closer?

The records are as of 31 December 2025, so any date up to then is in the past.

Rules:
- Line 1 is a verdict in bold: **No red flags found**, or **3 issues need review** (with the real number, never "N").
- A red flag is something that could stop the deal: winding up, a failed or doubtful audit, court cases
  against the company that are still pending, losses, negative equity, a big fall in revenue, or missing financials.
  Normal things (who owns the shares, recent director changes) are not red flags.
- Zero court cases is not missing data.
- Then the red flags as bullet points, most serious first. Leave this out if there are none.
- Then 1-2 sentences of context: what the company does, its size and trend, its age.
- Say plainly if data is missing.
- 120 words at most. Markdown.
- Only use facts you were given. Never invent or round numbers differently. Never recommend accepting or rejecting."""

V1_PROMPT = RULES + "\n\nHere is the full company record as JSON:\n{record}"

V2_PROMPT = (
    RULES
    + """

The red flags below were checked by code and are correct. Mention every one of them, and no others.
Red flags:
{flags}

Key facts:
{facts}"""
)

V3_EXTRA = """

The verdict line is already written. Do NOT write a verdict line: start directly with the bullet points
(or with the context sentences if there are no red flags)."""


V4_PROMPT = """You write one part of a short company summary for a team that vets companies before choosing them
as suppliers or partners. The verdict and the list of red flags are already written by code; do not repeat them.

Write 1-2 sentences (at most 45 words) of plain context: what the company does, where, how old it is,
its size (latest revenue) and its revenue trend. Use only the facts below, written exactly as given.
Do not mention red flags, risks, court cases or whether data is complete or missing. No headings, no bullets.

Facts:
{facts}"""


def build_prompt(version: str, record: dict) -> str:
    if version == "v4":
        return V4_PROMPT.format(facts=json.dumps(key_facts(record), indent=1))
    if version == "v1":
        return V1_PROMPT.format(record=json.dumps(record))
    flags = red_flags(record)
    flag_text = "\n".join(f"- {f['detail']}" for f in flags) or "(none)"
    prompt = V2_PROMPT.format(flags=flag_text, facts=json.dumps(key_facts(record), indent=1))
    if version == "v3":
        prompt += V3_EXTRA
    return prompt


def verdict(record: dict) -> str:
    """The first line of a v3 summary, written by code so it is always right."""
    n = len(red_flags(record))
    if n == 0:
        return "**No red flags found**"
    return f"**{n} issue{'s' if n > 1 else ''} need{'' if n > 1 else 's'} review**"


def finish(version: str, record: dict, llm_text: str) -> str:
    """Put together the final summary. v1 and v2 are all LLM; v3 and v4 add parts written by code."""
    if version == "v4":
        bullets = "\n".join(f"- {f['detail']}" for f in red_flags(record))
        parts = [verdict(record), bullets, llm_text.strip()]
        return "\n\n".join(p for p in parts if p)
    if version != "v3":
        return llm_text
    lines = llm_text.strip().splitlines()
    if lines and lines[0].startswith("**") and ("issue" in lines[0].lower() or "red flag" in lines[0].lower()):
        lines = lines[1:]
    return verdict(record) + "\n\n" + "\n".join(lines).strip()


# ---------- Calling the LLM ----------


def call_llm(client: Groq, prompt: str) -> str:
    """Send one prompt. If we hit the free tier's rate limit, wait and try again."""
    for attempt in range(5):
        try:
            extra = {}
            if MODEL.startswith("openai/gpt-oss"):
                # gpt-oss "thinks" before answering. Low effort = fewer tokens, so the free quota lasts longer.
                extra["reasoning_effort"] = "low"
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,  # 0 = same input gives (almost) the same output, so runs are comparable
                **extra,
            )
            text = response.choices[0].message.content or ""
            # Some models put their thinking inside <think>...</think>. The reader should not see it.
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        except RateLimitError:
            wait = 20 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
    raise RuntimeError("Still rate limited after 5 tries. Try again later.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", nargs="?", choices=["v1", "v2", "v3", "v4"])
    parser.add_argument("--list-models", action="store_true", help="print the models your key can use, then stop")
    parser.add_argument("--limit", type=int, help="only summarise the first N companies")
    args = parser.parse_args()
    if args.list_models:
        print(sorted(m.id for m in Groq().models.list().data))
        return
    if not args.version:
        parser.error("choose a version: v1, v2, v3 or v4")

    records = [json.loads(line) for line in COMPANIES.read_text(encoding="utf-8").splitlines()]
    if args.limit:
        records = records[: args.limit]

    client = Groq()  # uses GROQ_API_KEY
    run_dir = RUNS / args.version
    run_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for i, record in enumerate(records, 1):
        reg = record["registrationNumber"]
        cache = run_dir / f"{reg}.json"
        if cache.exists():  # already done: re-use the saved response
            saved = json.loads(cache.read_text(encoding="utf-8"))
        else:
            prompt = build_prompt(args.version, record)
            start = time.time()
            summary = call_llm(client, prompt)
            saved = {
                "registrationNumber": reg,
                "model": MODEL,
                "prompt": prompt,
                "summary": summary,
                "seconds": round(time.time() - start, 2),
            }
            cache.write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[{i}/{len(records)}] {reg} done")
        summary = finish(args.version, record, saved["summary"])
        lines.append(json.dumps({"registrationNumber": reg, "summary": summary}, ensure_ascii=False))

    SUMMARIES.mkdir(exist_ok=True)
    out = SUMMARIES / f"{args.version}.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved {len(lines)} summaries to {out}. Refresh the viewer to see them.")


if __name__ == "__main__":
    main()
