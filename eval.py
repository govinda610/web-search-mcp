"""How often the tools get the right answer: SimpleQA (short facts, answered with web_search)
and FRAMES (multi-hop, answered with deep_research), graded by an LLM judge.

Run: uv run eval.py simpleqa 50     or     uv run eval.py frames 20
The same seed picks the same questions every run, so scores are comparable across changes.
Needs a configured LLM (for the answers and the judge). Per-question results go to state/eval/."""
import asyncio
import csv
import io
import json
import random
import sys
import time

import httpx

import llm
import server
from store import STATE

DATASETS = {
    "simpleqa": ("https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv", ",", "problem", "answer"),
    "frames": ("https://huggingface.co/datasets/google/frames-benchmark/resolve/main/test.tsv", "\t", "Prompt", "Answer"),
}
JUDGE = """Grade a predicted answer against the gold answer.
CORRECT: it contains the gold answer's meaning without contradicting it (extra detail is fine).
INCORRECT: it gives a different or contradicting answer.
NOT_ATTEMPTED: it doesn't commit to an answer.
Question: {question}
Gold answer: {gold}
Predicted answer: {predicted}
Reply with exactly one word: CORRECT, INCORRECT or NOT_ATTEMPTED."""


def load(name: str) -> list[tuple[str, str]]:
    url, delimiter, q_col, a_col = DATASETS[name]
    path = STATE / "eval" / f"{name}.{'tsv' if delimiter == chr(9) else 'csv'}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(httpx.get(url, follow_redirects=True, timeout=60).raise_for_status().text)
    rows = csv.DictReader(io.StringIO(path.read_text()), delimiter=delimiter)
    return [(r[q_col].strip(), r[a_col].strip()) for r in rows]


async def answer(name: str, question: str) -> str:
    if name == "simpleqa":
        out = await server.web_search(question, depth="advanced", answer=True)
        return out.split("\n\n1. ", 1)[0] if out.startswith("ANSWER") else ""  # the answer, without the results
    return await server.deep_research(question, report=True)


async def grade(question: str, gold: str, predicted: str) -> str:
    if not predicted:
        return "NOT_ATTEMPTED"
    verdict = await llm.ask(JUDGE.format(question=question, gold=gold, predicted=predicted[:6000]), max_tokens=1500)
    return next((v for v in ("NOT_ATTEMPTED", "INCORRECT", "CORRECT") if v in (verdict or "").upper()), "UNGRADED")


async def main(name: str, n: int) -> None:
    if not llm.llm_available():
        sys.exit("No LLM configured: the answers and the judge both need one.")
    questions = random.Random(0).sample(load(name), n)
    log = STATE / "eval" / f"{name}-{time.strftime('%Y%m%d-%H%M')}.jsonl"
    counts: dict[str, int] = {}
    with log.open("w") as f:
        for i, (question, gold) in enumerate(questions, 1):
            try:
                predicted = await answer(name, question)
            except Exception as e:  # noqa: BLE001 - a failed question counts as not attempted
                predicted = ""
                print(f"  error: {e}"[:200])
            verdict = await grade(question, gold, predicted)
            counts[verdict] = counts.get(verdict, 0) + 1
            f.write(json.dumps({"question": question, "gold": gold, "predicted": predicted, "verdict": verdict}) + "\n")
            print(f"[{i}/{n}] {verdict}: {question[:80]}")
    correct, attempted = counts.get("CORRECT", 0), n - counts.get("NOT_ATTEMPTED", 0)
    print(f"\n{name}: {correct}/{n} correct ({correct / n:.0%}), "
          f"{correct}/{attempted or 1} of attempted ({correct / (attempted or 1):.0%}). {counts}\nlog: {log}")


if __name__ == "__main__":
    dataset = sys.argv[1] if len(sys.argv) > 1 else "simpleqa"
    if dataset not in DATASETS:
        sys.exit(f"usage: uv run eval.py [{'|'.join(DATASETS)}] [number of questions]")
    asyncio.run(main(dataset, int(sys.argv[2]) if len(sys.argv) > 2 else 20))
