"""Run the assistant over every question in KA_Test_Questions.xlsx.

Two layers of assessment:
  1. auto_check  - automated, repeatable regression check from eval_expectations.csv: did the
                   assistant answer/decline as expected, does the answer contain the key facts,
                   and does it avoid forbidden content (e.g. the injected "24 hours" claim).
                   Text is normalised (lower-case, alphanumerics only) so "R5,000,000" and
                   "R5 000 000" both match "5000000".
  2. assessment  - human verdict from assessments.csv (the authoritative mark, using the
                   workbook's dropdown values), written into the workbook.

Outputs
  results.csv                       one row per question (answers from run 1)
  KA_Test_Questions_completed.xlsx  original workbook with the yellow columns filled
  stability.csv                     only with --runs N > 1: auto_check pass rate per question

Usage
  python evaluate.py              run all questions once
  python evaluate.py --runs 3     run each question 3 times to measure consistency
  python evaluate.py --reuse      re-apply assessments.csv to saved answers (no LLM calls)
Exit code is 1 if any auto_check fails, so this can gate CI.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from openpyxl import load_workbook

from rag.engine import MODEL, Engine, LLMError

ROOT = Path(__file__).parent
SRC = ROOT / "KA_Test_Questions.xlsx"
OUT_XLSX = ROOT / "KA_Test_Questions_completed.xlsx"
OUT_CSV = ROOT / "results.csv"
STABILITY_CSV = ROOT / "stability.csv"
ASSESS_CSV = ROOT / "assessments.csv"
EXPECT_CSV = ROOT / "eval_expectations.csv"
ALLOWED = {"Correct", "Partially correct", "Incorrect", "Correctly declined", "Incorrectly declined"}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def read_csv(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["id"].strip(): r for r in csv.DictReader(f)}


def load_assessments() -> dict:
    rows = read_csv(ASSESS_CSV)
    for k, r in rows.items():
        if r["assessment"] and r["assessment"] not in ALLOWED:
            sys.exit(f"{ASSESS_CSV.name} {k}: '{r['assessment']}' is not one of {sorted(ALLOWED)}")
    return rows


def auto_check(expect: dict | None, answered: bool, answer: str) -> tuple[str, str]:
    """Return ('PASS'|'FAIL'|'n/a', reason)."""
    if not expect:
        return "n/a", "no expectation defined"
    if expect["expect"] == "decline":
        return ("PASS", "declined as expected") if not answered else ("FAIL", "answered; expected decline")
    if not answered:
        return "FAIL", "declined; expected an answer"
    a = norm(answer)
    missing = [t for t in expect["must_contain"].split("|") if t and t not in a]
    forbidden = [t for t in expect["must_not_contain"].split("|") if t and t in a]
    if missing or forbidden:
        return "FAIL", "; ".join(filter(None, [missing and f"missing {missing}",
                                               forbidden and f"contains forbidden {forbidden}"]))
    return "PASS", "key facts present"


def find_header(ws):
    """Locate the header row and columns by their labels."""
    for row in ws.iter_rows():
        labels = {str(c.value).strip().lower(): c.column for c in row if c.value}
        if "id" in labels and "question" in labels:
            col = lambda prefix: next(v for k, v in labels.items() if k.startswith(prefix))  # noqa: E731
            return row[0].row, {
                "id": labels["id"], "question": labels["question"],
                "answer": col("your system"), "sources": col("sources"),
                "assessment": col("your assessment"), "comment": col("comments"),
            }
    sys.exit("Header row with 'ID' and 'Question' not found.")


def ask_once(engine: Engine, qid: str, question: str) -> dict:
    t0 = time.perf_counter()
    res = engine.ask(question)
    usage = res.get("usage", {})
    return {
        "id": qid, "question": question, "answered": res["found"], "answer": res["answer"],
        "sources_cited": "; ".join(s["citation"] for s in res["sources"]) or "None (declined)",
        "chunks_retrieved": " | ".join(res.get("retrieved", [])),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_s": round(time.perf_counter() - t0, 2),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--runs", type=int, default=1, help="times to ask each question (default 1)")
    ap.add_argument("--reuse", action="store_true", help="re-use answers in results.csv; no LLM calls")
    args = ap.parse_args()

    if not SRC.exists():
        sys.exit(f"Put {SRC.name} in the project folder first.")
    wb = load_workbook(SRC)
    ws = wb["Test Questions"]
    hdr, cols = find_header(ws)
    verdicts, expectations = load_assessments(), read_csv(EXPECT_CSV)

    if args.reuse:
        previous = read_csv(OUT_CSV)
        print(f"Re-using answers from {OUT_CSV.name} (no LLM calls)\n")
    else:
        engine = Engine()
        print(f"Model: {MODEL} | indexed chunks: {len(engine.chunks)} | runs: {args.runs}\n")

    results, stability = [], []
    for r in range(hdr + 1, ws.max_row + 1):
        qid = str(ws.cell(r, cols["id"]).value or "").strip()
        question = ws.cell(r, cols["question"]).value
        if not qid.upper().startswith("Q") or not question:   # skip the 'Ex' example row
            continue
        exp = expectations.get(qid)

        if args.reuse:
            runs = [dict(previous[qid])]
        else:
            try:
                runs = [ask_once(engine, qid, str(question)) for _ in range(args.runs)]
            except LLMError as e:
                sys.exit(f"{qid}: {e}")
        for run in runs:
            run["auto_check"], run["auto_reason"] = auto_check(
                exp, str(run["answered"]) == "True", run["answer"])

        row = runs[0]
        v = verdicts.get(qid, {})
        row["assessment"], row["comment"] = v.get("assessment", ""), v.get("comment", "")
        results.append(row)
        if args.runs > 1:
            passes = sum(x["auto_check"] == "PASS" for x in runs)
            stability.append({"id": qid, "runs": len(runs), "auto_pass": passes,
                              "answered": sum(str(x["answered"]) == "True" for x in runs),
                              "pass_rate": f"{passes / len(runs):.0%}"})

        ws.cell(r, cols["answer"]).value = row["answer"]
        ws.cell(r, cols["sources"]).value = row["sources_cited"]
        ws.cell(r, cols["assessment"]).value = row["assessment"] or None
        ws.cell(r, cols["comment"]).value = row["comment"] or None
        rate = f" ({stability[-1]['pass_rate']} of {args.runs})" if args.runs > 1 else ""
        print(f"{qid:>4} {'answered' if str(row['answered']) == 'True' else 'DECLINED':>8} "
              f"auto={row['auto_check']}{rate:<12} human={row['assessment'] or '-':<20} {str(question)[:45]}")

    wb.save(OUT_XLSX)
    write_csv(OUT_CSV, results)
    if stability:
        write_csv(STABILITY_CSV, stability)

    n = len(results)
    passed = sum(x["auto_check"] == "PASS" for x in results)
    tin = sum(int(x["input_tokens"]) for x in results)
    tout = sum(int(x["output_tokens"]) for x in results)
    print(f"\nAuto-check: {passed}/{n} PASS")
    for x in results:
        if x["auto_check"] == "FAIL":
            print(f"  {x['id']}: {x['auto_reason']}")
    print(f"Tokens: avg {tin // n} in / {tout // n} out per question")
    print(f"Wrote {OUT_CSV.name}, {OUT_XLSX.name}" + (f", {STABILITY_CSV.name}" if stability else ""))
    missing = [x["id"] for x in results if not x["assessment"]]
    if missing:
        print(f"No human verdict in {ASSESS_CSV.name} for: {', '.join(missing)}")
    sys.exit(1 if passed < n else 0)


if __name__ == "__main__":
    main()
