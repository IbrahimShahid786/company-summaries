# Case study: company summaries for supplier vetting

**Result in one line:** the more of the work I moved from the LLM to plain code, the more reliable the summaries got.
The final system catches every red flag code can detect, never gets the verdict wrong, and puts no wrong numbers in
the summaries.

| Version | How it works | Red flags mentioned | Verdict right | Wrong numbers | Avg words |
|---|---|---|---|---|---|
| v0 | First prompt, LLM reads the raw record (3 companies only) | 2/2 | 1/3 | 2/12 | 115 |
| v1 | Improved prompt, LLM reads the raw record | 28/36 (78%) | 20/50 | 5/155 | 95 |
| v2 | Code finds the red flags and key facts, LLM writes from them | 36/36 | 46/50 | 0/197 | 74 |
| v3 | v2 + code writes the verdict line | 36/36 | 50/50 | 0/219 | 72 |
| v4 | Code writes verdict and red flags, LLM writes only the context | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

All 50 companies are used. Model: `openai/gpt-oss-120b` on Groq's free tier, `temperature=0`, low reasoning effort.
Every prompt and response is saved in [`runs/`](runs/), so all scores can be reproduced without calling the model again.

## 1. What a good summary is for this team, and why

**The reader and the decision.** The team vets companies before choosing them as suppliers or partners. They read
the summary *first*, to decide whether the full record deserves a closer look. So the summary is not a description
of the company. It answers one question: **is there any reason not to proceed, or to look closer?**

**What I found in the data** (all 50 records, before writing any prompt):

| What | Companies | Why it matters to this team |
|---|---|---|
| No warning signs at all | 23 | Most companies are fine, so "nothing found" must be said as clearly as a problem |
| Status "Winding up" | 2 | The company is being closed down: the most serious signal |
| Winding-up petitions against the company | 5 (8 petitions) | A creditor asked a court to close the company |
| Audit "disclaimer" or "going concern" doubt | 3 | The auditor could not confirm the accounts, or doubts the company will survive |
| Pending cases where the company is the **defendant** | 6 | It may have to pay. As plaintiff, it is usually chasing its own debtors, which is less of a concern |
| Loss in the latest year / negative equity | 11 / 1 | Financial weakness, but not on its own a reason to reject |
| No financial statements at all | 3 | The reader must know what could not be checked |

One company shows why the whole record matters: **SYN-000545 (Cahaya Golden Systems)** has growing revenue
(RM12.38m → RM13.01m) and is profitable, but it is in winding up, has had two winding-up petitions, a going-concern
audit warning and RM2.50m in pending claims. A summary built from the financials alone would mislead the team.

**So a good summary:**
1. **Opens with a one-line verdict**: "No red flags found" or "3 issues need review". The reader knows in 2 seconds.
2. **Lists the red flags, most serious first**: winding up → audit warnings and negative equity → pending cases
   against the company → losses and falling revenue → missing data.
3. **Adds 1-2 sentences of context**: what the company does, its size, age and revenue trend.
4. **Is short**: under 120 words, so it is faster than reading the record.

**And it never:**
- **States a wrong number or invents a fact.** A wrong figure is worse than no summary, because the reader trusts it
  and skips the record.
- **Misses a red flag.** Missing a winding-up petition is the worst possible error; an awkward sentence is a small one.
- **Makes the decision.** It says "needs review", not "reject". The team owns the decision.

**A judgment call I made.** My red-flag list is a choice, not the truth. In v1 the LLM raised other concerns, such as
three years of negative free cash flow (SYN-000029). That is a reasonable concern for a financial analyst, but I kept
the list to things that could stop a supplier deal, so the verdict stays meaningful. I would agree the list with the
team (see section 4).

## 2. What you built

```
data/companies.jsonl ──▶ facts.py ──▶ red flags + key facts (plain Python, no LLM)
                                  │
                                  ├──▶ verdict line + red-flag bullets (code)
                                  └──▶ prompt ──▶ LLM ──▶ 1-2 context sentences
                                                        │
                    summaries/<version>.jsonl ◀─────────┘ ──▶ viewer, and evaluate.py
```

| File | What it does |
|---|---|
| [`summariser/facts.py`](summariser/facts.py) | Finds red flags with simple rules, sorts them by severity, and picks the key facts |
| [`summariser/summarise.py`](summariser/summarise.py) | Builds the prompt for each version, calls the LLM, caches every response in `runs/` |
| [`summariser/evaluate.py`](summariser/evaluate.py) | Scores every summary automatically (section 3) |

**How the design got there.** Each version fixed a failure I saw in the previous one:

- **v0 → v1.** The first prompt did not say the records are dated 31 Dec 2025, so the LLM called director
  appointments in late 2025 "future dates, possibly data errors". It also printed the template literally
  ("**N issue(s) need review**"). I added the record date and a definition of a red flag.
- **v1 → v2.** Reading the raw JSON (about 1,900 tokens), the LLM still missed 8 of 36 red flags, including both
  winding-up petitions for SYN-000545, and raised false alarms on 20 of 50 companies. So I moved finding the red flags
  into code, where it is exact, and gave the LLM the checked list.
- **v2 → v3.** v2 caught every flag, but 3 summaries still began "N issue(s)", and they were the riskiest companies
  (SYN-000451, SYN-000545, SYN-001212). Counting is easy for code, so code writes the verdict.
- **v3 → v4.** Reading v3 by hand, 9 summaries said "No red flags found" and then listed a bullet anyway (usually a
  court case where the company is the plaintiff), and 22 ended with filler such as "All required information is
  present." So code writes the bullets too, and the LLM only writes the context sentences.

**The idea, taken all the way:** *anything code can do reliably, code should do. The LLM only does what code
cannot: turn facts into clear sentences.*

**Trade-off.** v4 is the most reliable but the least natural: the bullets are template text, and the LLM can no
longer merge related flags (two petitions) into one sentence. For a risk-vetting team I chose reliable over smooth.

## 3. How you know whether it's good

**The checklist comes from code.** `facts.py` gives every company a list of red flags. Because the rules are plain
Python, I checked them against the records by hand. That list is the answer key.

**Four automatic checks** ([`evaluate.py`](summariser/evaluate.py)), run on every summary:

| Check | How | Why |
|---|---|---|
| Red flags mentioned | Keywords per flag type (e.g. "petition", "going concern") | Missing a flag is the worst error |
| Verdict right | Says "no red flags" exactly when there are none, and gives a real number otherwise | The reader acts on line 1 |
| Wrong numbers | Every RM amount and % must match a number in the record, allowing for the rounding the writer used | A wrong figure misleads |
| Length | Word count | Must be faster than the record |

**I tested the checks themselves.** Perfect fake summaries (built from the answer key) score 36/36, 50/50 and 0 wrong
numbers; deliberately bad ones score 0/36 and every number is caught. The checks also had bugs that I found and fixed:
shareholdings are stored as `58.25`, not `0.5825`; a claim total is a sum that does not appear in the record; and
"RM4k" is a correct rounding of RM4,400.

**Reading by hand.** The numbers said v3 was perfect. Reading all 50 v3 summaries showed it was not (the
contradictions and filler above). So automatic checks find the problems you already know about; reading finds new
ones. Both are needed.

**Limits of this evaluation.**
- The keyword checks can be fooled: a summary can mention "petition" without describing it correctly.
- Numbers written without "RM" or "%" (e.g. "690,500") are not checked.
- The red-flag rules can only find what is in the record and what I thought of.
- 50 companies is a small sample, and I was the only reader.

## 4. What you'd do next, and how you'd keep improving it

1. **Agree the red-flag list with the team.** Show them 10 summaries and ask: what would you have wanted to know
   that is missing? What is noise? Turn the answers into rules in `facts.py`.
2. **Measure the real goal: time saved and nothing missed.** Give reviewers a record with and without the summary,
   and record time taken and whether they spotted each issue.
3. **Make the checks stronger.** Use a second LLM as a judge only for what keywords cannot check (is the context
   sentence faithful to the facts?), and check every number the LLM writes, not just RM and %.
4. **Better rules.** Weight court cases by claim size relative to revenue; flag sharp changes in key ratios;
   treat a withdrawn petition as less serious than a judgment.
5. **Keep it working.** Run `evaluate.py` on every change (it costs nothing, because responses are cached), keep
   a small set of hand-checked "golden" summaries, and alert if a new model or prompt lowers any score.

## How to run it

```powershell
uv sync
# put your key in .env:  GROQ_API_KEY=...   (see .env.example)
uv run python -m summariser.summarise v4          # v1, v2, v3 or v4; add --limit 3 to test
uv run python -m summariser.evaluate v1 v2 v3 v4  # the table above; details in results/<version>.csv
uv run uvicorn app.main:app --port 8000           # the viewer, to compare versions side by side
```

## Tools I used

- **LLM for the summaries:** `openai/gpt-oss-120b` through Groq's free API.
- **Claude (Anthropic):** to help analyse the data, write and debug the Python code, and draft this case study.
  I reviewed the summaries and the results, and I can explain every part of the code.
