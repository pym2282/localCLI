"""
compare.py  --  A/B comparison tool for OCLI using Ollama as judge

Commands:
  baseline          Snapshot current state into Origin/ (run this first)

  prompt [a] [b]    Compare two system prompt files
                    default: Origin/OCLI.md  vs  Temp/OCLI.md

  code   [a] [b]    Compare two directory versions of the CLI
                    default: Origin/          vs  Temp/

Flags:
  --claude-review   Full auto pipeline: Ollama judging → Claude final review
                    via Claude Code CLI (uses your existing subscription).
                    On REJECT: candidate auto-reverted from baseline.
                    Every run saved to history/.

Test types in compare_suite.json:
  single   One user prompt → one response
  multi    Multiple turns → tests context retention across turns
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3-14b-q4km"
SUITE_PATH = Path(__file__).parent / "compare_suite.json"

SYSTEM_PROMPT_BASE = (
    "You are OCLI, a local coding and research assistant.\n"
    "Your full instructions are provided below from OCLI.md.\n"
    "Follow them exactly."
)

JUDGE_SYSTEM = (
    "You are an impartial AI response evaluator. "
    "Reply only in valid JSON with no extra text."
)

JUDGE_SINGLE = """\
A user asked a question and received two AI assistant responses. Evaluate them.

User question:
{prompt}

--- Response A ---
{response_a}

--- Response B ---
{response_b}

Score each from 1 to 10 for accuracy, conciseness, and helpfulness.
Declare a winner.

Reply in this exact JSON (nothing else):
{{"score_a": 0, "score_b": 0, "winner": "A", "reason": "one sentence"}}

winner must be exactly "A", "B", or "TIE"."""

JUDGE_MULTI = """\
A user had a multi-turn conversation with an AI assistant. \
Evaluate the full exchange from both versions.

Conversation turns:
{turns}

--- Version A (full exchange) ---
{response_a}

--- Version B (full exchange) ---
{response_b}

Focus on: context retention between turns, consistency, accuracy, \
building correctly on previous answers.
Score each from 1 to 10.

Reply in this exact JSON (nothing else):
{{"score_a": 0, "score_b": 0, "winner": "A", "reason": "one sentence"}}

winner must be exactly "A", "B", or "TIE"."""


# ── Ollama helpers ──────────────────────────────────────────────────────────

def _call_ollama(messages: list[dict], timeout: int = 120) -> str:
    payload = {"model": MODEL, "messages": messages, "stream": False}
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=(10, timeout))
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def _build_conversation(system: str, turns: list[str]) -> str:
    """Run multi-turn conversation directly via Ollama, return full exchange text."""
    messages = [{"role": "system", "content": system}]
    exchange_parts = []

    for turn in turns:
        messages.append({"role": "user", "content": turn})
        reply = _call_ollama(messages, timeout=120)
        # strip <think> blocks
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        messages.append({"role": "assistant", "content": reply})
        exchange_parts.append(f"User: {turn}\nAssistant: {reply}")

    return "\n\n".join(exchange_parts)


def _single_call(system: str, prompt: str) -> str:
    raw = _call_ollama([
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ])
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


# ── CLI subprocess helpers (code mode) ─────────────────────────────────────

def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _extract_all_responses(raw: str) -> str:
    """Extract every MODEL: section from CLI stdout."""
    clean = _strip_ansi(raw)
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL)
    chunks = re.findall(r"MODEL:\n\n?(.*?)(?:\n\n>|\Z)", clean, re.DOTALL)
    return "\n\n---\n\n".join(c.strip() for c in chunks) if chunks else clean.strip()


def _run_cli(script_dir: Path, inputs: list[str], timeout: int = 120) -> str:
    env = {**os.environ, "OCLI_EVAL": "1"}
    stdin_text = "\n".join(inputs) + "\nexit\n"
    try:
        result = subprocess.run(
            [sys.executable, "main.py"],
            cwd=str(script_dir),
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        raw = result.stdout.decode("utf-8", errors="replace")
        return _extract_all_responses(raw)
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERROR: {e}]"


# ── Judge ───────────────────────────────────────────────────────────────────

def judge(test: dict, response_a: str, response_b: str) -> dict:
    if response_a.startswith("[") or response_b.startswith("["):
        return {"score_a": 0, "score_b": 0, "winner": "TIE",
                "reason": "one or both sides errored"}

    if test.get("type") == "multi":
        turns_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(test["turns"]))
        content = JUDGE_MULTI.format(
            turns=turns_text,
            response_a=response_a,
            response_b=response_b,
        )
    else:
        content = JUDGE_SINGLE.format(
            prompt=test["prompt"],
            response_a=response_a,
            response_b=response_b,
        )

    raw = _call_ollama([
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user",   "content": content},
    ], timeout=120)

    match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if result.get("winner") not in ("A", "B", "TIE"):
                result["winner"] = "TIE"
            return result
        except json.JSONDecodeError:
            pass

    return {"score_a": 0, "score_b": 0, "winner": "TIE",
            "reason": f"parse error: {raw[:80]}"}


# ── Aggregation ─────────────────────────────────────────────────────────────

def _print_summary(results: list[dict], label_a: str, label_b: str) -> None:
    wins_a = sum(1 for r in results if r["winner"] == "A")
    wins_b = sum(1 for r in results if r["winner"] == "B")
    ties   = sum(1 for r in results if r["winner"] == "TIE")
    avg_a  = sum(r.get("score_a", 0) for r in results) / max(len(results), 1)
    avg_b  = sum(r.get("score_b", 0) for r in results) / max(len(results), 1)

    w = max(len(label_a), len(label_b), 24)
    print("\n" + "=" * (w + 28))
    print(f"  {'A: ' + label_a:<{w}}  avg={avg_a:.1f}  wins={wins_a}")
    print(f"  {'B: ' + label_b:<{w}}  avg={avg_b:.1f}  wins={wins_b}")
    print(f"  {'ties':<{w}}  {ties}")
    print("=" * (w + 28))
    if wins_a > wins_b:
        print(f"  WINNER: A ({label_a})")
    elif wins_b > wins_a:
        print(f"  WINNER: B ({label_b})")
    else:
        print("  WINNER: TIE")
    print()


# ── Claude review ──────────────────────────────────────────────────────────

def _get_file_diff(path_a: Path, path_b: Path) -> str:
    try:
        lines_a = path_a.read_text(encoding="utf-8").splitlines(keepends=True)
        lines_b = path_b.read_text(encoding="utf-8").splitlines(keepends=True)
        return "".join(difflib.unified_diff(lines_a, lines_b,
                                            fromfile=str(path_a),
                                            tofile=str(path_b)))
    except Exception as e:
        return f"[diff error: {e}]"


def _get_dir_diff(dir_a: Path, dir_b: Path) -> str:
    parts = []
    for fname in BASELINE_FILES:
        d = _get_file_diff(dir_a / fname, dir_b / fname)
        if d.strip():
            parts.append(d)
    return "\n".join(parts) if parts else "[no differences found]"


def _save_history(entry: dict) -> Path:
    HISTORY_DIR.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = HISTORY_DIR / f"{ts}.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_recent_history(limit: int = 5) -> list[dict]:
    if not HISTORY_DIR.exists():
        return []
    files = sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def _do_revert(src: Path, dst: Path) -> list[str]:
    reverted = []
    if src.is_dir() and dst.is_dir():
        for fname in BASELINE_FILES:
            s = src / fname
            if s.exists():
                shutil.copy2(s, dst / fname)
                reverted.append(fname)
    elif src.is_file():
        shutil.copy2(src, dst)
        reverted.append(src.name)
    return reverted


def claude_review(diff_text: str, results: list[dict],
                  label_a: str, label_b: str,
                  revert_src: Path | None = None,
                  revert_dst: Path | None = None) -> None:
    wins_a = sum(1 for r in results if r["winner"] == "A")
    wins_b = sum(1 for r in results if r["winner"] == "B")
    ties   = sum(1 for r in results if r["winner"] == "TIE")
    avg_a  = sum(r.get("score_a", 0) for r in results) / max(len(results), 1)
    avg_b  = sum(r.get("score_b", 0) for r in results) / max(len(results), 1)

    score_block = (
        f"A ({label_a}): avg={avg_a:.1f}, wins={wins_a}\n"
        f"B ({label_b}): avg={avg_b:.1f}, wins={wins_b}\n"
        f"ties={ties}, total_tests={len(results)}"
    )

    history = _load_recent_history(limit=5)
    history_block = ""
    if history:
        lines = []
        for h in history:
            v = h.get("verdict", "?")
            r = h.get("reason", "")[:200]
            lines.append(f"- [{v}] {r}")
        history_block = (
            "\n## Recent prior reviews (most recent first)\n"
            + "\n".join(lines)
            + "\n\nIf the current diff repeats a previously REJECTED pattern, "
              "weigh that heavily.\n"
        )

    prompt = f"""You are reviewing a proposed improvement to OCLI, a local AI CLI assistant.

## Diff  (A = baseline → B = candidate)
```diff
{diff_text[:8000]}
```

## Ollama Judge Results (A vs B)
{score_block}
{history_block}
Review the diff. Check for quality improvements, regressions, unintended side effects,
or incomplete changes. Then give a final verdict.

Respond ONLY in this exact JSON (no extra text):
{{"verdict": "APPROVE", "reason": "one or two sentences"}}

verdict must be exactly "APPROVE" or "REJECT"."""

    print("\n[Asking Claude for final review...]\n")

    verdict, reason = "ERROR", ""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        raw = result.stdout.strip()
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            data    = json.loads(m.group())
            v       = data.get("verdict", "UNKNOWN")
            verdict = v if v in ("APPROVE", "REJECT") else "UNKNOWN"
            reason  = data.get("reason", "")
        else:
            reason = f"parse error: {raw[:120]}"
    except Exception as e:
        reason = f"error: {e}"

    bar = "=" * 52
    print(f"\n{bar}")
    print(f"  CLAUDE VERDICT: {verdict}")
    print(f"  Reason: {reason}")
    print(f"{bar}")

    entry = {
        "timestamp":    datetime.now().isoformat(timespec="seconds"),
        "label_a":      label_a,
        "label_b":      label_b,
        "scores": {
            "avg_a": round(avg_a, 2), "avg_b": round(avg_b, 2),
            "wins_a": wins_a, "wins_b": wins_b, "ties": ties,
            "total_tests": len(results),
        },
        "diff_preview": diff_text[:1000],
        "verdict":      verdict,
        "reason":       reason,
        "reverted":     [],
    }

    if verdict == "REJECT" and revert_src and revert_dst:
        reverted = _do_revert(revert_src, revert_dst)
        entry["reverted"] = reverted
        print(f"  Auto-reverted: {', '.join(reverted) or 'nothing'}")

    hist_path = _save_history(entry)
    print(f"  History saved: {hist_path.name}\n")


# ── Prompt mode ─────────────────────────────────────────────────────────────

def compare_prompts(path_a: Path, path_b: Path, tests: list[dict], runs: int,
                    do_claude_review: bool = False) -> None:
    sys_a = SYSTEM_PROMPT_BASE + "\n\n" + path_a.read_text(encoding="utf-8")
    sys_b = SYSTEM_PROMPT_BASE + "\n\n" + path_b.read_text(encoding="utf-8")
    results = []

    for test in tests:
        tid  = test["id"]
        kind = test.get("type", "single")
        label = f"[{kind.upper()}] {tid}"
        preview = (test.get("prompt") or test["turns"][0])[:50]
        print(f"\n{label}: {preview}...")

        for run in range(runs):
            if kind == "multi":
                resp_a = _build_conversation(sys_a, test["turns"])
                resp_b = _build_conversation(sys_b, test["turns"])
            else:
                resp_a = _single_call(sys_a, test["prompt"])
                resp_b = _single_call(sys_b, test["prompt"])

            verdict = judge(test, resp_a, resp_b)
            results.append(verdict)
            w = verdict["winner"]
            print(f"  run {run+1}: A={verdict['score_a']} B={verdict['score_b']}"
                  f" → {w}  {verdict.get('reason','')[:55]}")

    _print_summary(results, str(path_a), str(path_b))
    if do_claude_review:
        claude_review(_get_file_diff(path_a, path_b), results,
                      str(path_a), str(path_b),
                      revert_src=path_a, revert_dst=path_b)


# ── Code mode ───────────────────────────────────────────────────────────────

def compare_code(dir_a: Path, dir_b: Path, tests: list[dict], runs: int,
                 do_claude_review: bool = False) -> None:
    results = []

    for test in tests:
        tid   = test["id"]
        kind  = test.get("type", "single")
        label = f"[{kind.upper()}] {tid}"
        turns = test["turns"] if kind == "multi" else [test["prompt"]]
        preview = turns[0][:50]
        print(f"\n{label}: {preview}...")

        for run in range(runs):
            print(f"  run {run+1}: A...", end="", flush=True)
            resp_a = _run_cli(dir_a, turns)
            print(" B...", end="", flush=True)
            resp_b = _run_cli(dir_b, turns)
            print(" judge...", end="", flush=True)
            verdict = judge(test, resp_a, resp_b)
            results.append(verdict)
            w = verdict["winner"]
            print(f" A={verdict['score_a']} B={verdict['score_b']}"
                  f" → {w}  {verdict.get('reason','')[:55]}")

    _print_summary(results, str(dir_a), str(dir_b))
    if do_claude_review:
        claude_review(_get_dir_diff(dir_a, dir_b), results,
                      str(dir_a), str(dir_b),
                      revert_src=dir_a, revert_dst=dir_b)


# ── Baseline ──────────────────��─────────────────────────────────────────────

ROOT        = Path(__file__).parent
ORIGIN_DIR  = ROOT / "Origin"
TEMP_DIR    = ROOT / "Temp"
HISTORY_DIR = ROOT / "history"

BASELINE_FILES = ["main.py", "tools.py", "OCLI.md"]


def cmd_baseline() -> None:
    ORIGIN_DIR.mkdir(exist_ok=True)
    copied = []
    for fname in BASELINE_FILES:
        src = ROOT / fname
        if src.exists():
            import shutil
            shutil.copy2(src, ORIGIN_DIR / fname)
            copied.append(fname)
    print(f"Origin/ updated: {', '.join(copied)}")
    print(f"Snapshot locked at: {ORIGIN_DIR}")


# ── Entry point ───────────────��─────────────────────────��───────────────────

def main() -> None:
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    tests = suite["test_prompts"]
    runs  = suite.get("runs_per_test", 1)

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    do_claude_review = "--claude-review" in args
    args = [a for a in args if a != "--claude-review"]

    mode = args[0]

    if mode == "baseline":
        cmd_baseline()
        return

    if mode == "prompt":
        path_a = Path(args[1]) if len(args) > 1 else ORIGIN_DIR / "OCLI.md"
        path_b = Path(args[2]) if len(args) > 2 else TEMP_DIR   / "OCLI.md"
        if not path_a.exists():
            sys.exit(f"Not found: {path_a}  (run 'python compare.py baseline' first?)")
        if not path_b.exists():
            sys.exit(f"Not found: {path_b}")
        print(f"\nPrompt comparison:")
        print(f"  A (baseline): {path_a}")
        print(f"  B (candidate): {path_b}")
        print(f"  tests={len(tests)}  runs/test={runs}")
        compare_prompts(path_a, path_b, tests, runs, do_claude_review)

    elif mode == "code":
        dir_a = Path(args[1]) if len(args) > 1 else ORIGIN_DIR
        dir_b = Path(args[2]) if len(args) > 2 else TEMP_DIR
        if not (dir_a / "main.py").exists():
            sys.exit(f"main.py not found in: {dir_a}  (run 'python compare.py baseline' first?)")
        if not (dir_b / "main.py").exists():
            sys.exit(f"main.py not found in: {dir_b}")
        print(f"\nCode comparison:")
        print(f"  A (baseline): {dir_a}")
        print(f"  B (candidate): {dir_b}")
        print(f"  tests={len(tests)}  runs/test={runs}")
        compare_code(dir_a, dir_b, tests, runs, do_claude_review)

    else:
        sys.exit(f"Unknown command '{mode}'. Use: baseline | prompt | code")


if __name__ == "__main__":
    main()
