"""
improve.py  --  Autonomous improvement loop for OCLI

Each cycle:
  1. Claude researches (WebSearch) and improves Temp/ based on rejection history
  2. compare.py tests Temp/ vs Origin/ and asks Claude to APPROVE/REJECT
  3. APPROVE → promote Temp/ to root, update Origin/ baseline, continue
     REJECT  → Temp/ auto-reverted by compare.py, try again
  4. Stop when: APPROVE streak >= 2 (converged) or REJECT streak >= 4 (stuck)
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT        = Path(__file__).parent
ORIGIN_DIR  = ROOT / "Origin"
TEMP_DIR    = ROOT / "Temp"
HISTORY_DIR = ROOT / "history"

BASELINE_FILES = ["main.py", "tools.py", "OCLI.md"]

MAX_CYCLES     = 15
APPROVE_STREAK = 2   # consecutive APPROVEs → converged
REJECT_STREAK  = 4   # consecutive REJECTs  → stuck


# ── helpers ────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 600) -> tuple[str, str, int]:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def _load_recent_history(limit: int = 8) -> list[dict]:
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


def _last_verdict() -> str:
    h = _load_recent_history(1)
    return h[0].get("verdict", "") if h else ""


def _reject_reasons(limit: int = 5) -> str:
    history = _load_recent_history(limit)
    rejects = [h for h in history if h.get("verdict") == "REJECT"]
    if not rejects:
        return ""
    return "\n".join(
        f"- {h.get('reason', '')}" for h in rejects
    )


def _promote() -> None:
    """Copy Temp/ → root files (make improvement permanent)."""
    for fname in BASELINE_FILES:
        src = TEMP_DIR / fname
        if src.exists():
            shutil.copy2(src, ROOT / fname)
    print(f"  Promoted: {', '.join(BASELINE_FILES)}")


def _update_baseline() -> None:
    """Re-snapshot root → Origin/ so next cycle compares against new baseline."""
    out, _, _ = _run([sys.executable, "compare.py", "baseline"])
    print(f"  {out}")


def _sync_temp_from_origin() -> None:
    """Reset Temp/ from Origin/ before each improvement attempt."""
    for fname in BASELINE_FILES:
        src = ORIGIN_DIR / fname
        if src.exists():
            shutil.copy2(src, TEMP_DIR / fname)


# ── improvement step ────────────────────────────────────────────────────────

def _build_improve_prompt(cycle: int) -> str:
    reject_block = _reject_reasons()
    reject_section = (
        f"\n## Previous REJECT reasons (address these)\n{reject_block}\n"
        if reject_block else
        "\n## No prior rejections — focus on general quality improvements.\n"
    )

    return f"""You are autonomously improving OCLI — a local coding and research assistant CLI
that runs on Ollama (qwen3-14b). This is improvement cycle {cycle}.

Your job:
1. SEARCH for topics relevant to the improvement areas below.
2. For each search result, use FETCH_URL on the most promising URLs to read the actual content.
   Extract concrete techniques, patterns, or code snippets — not just summaries.
3. Read the current baseline files in {ORIGIN_DIR.as_posix()}/
4. Apply targeted improvements by editing files in {TEMP_DIR.as_posix()}/
   (edit OCLI.md for prompt changes, main.py/tools.py for code changes)

Research depth expected:
- At least 2-3 searches on different sub-topics
- Follow at least 2-3 URLs per search to get real content
- Base changes on what you actually read, not general knowledge

Focus areas based on what a local Ollama CLI assistant needs most:
- System prompt clarity and instruction following
- Context/memory management across long sessions
- Tool use reliability and loop prevention
- Response quality and conciseness
- Robustness (edge cases, error handling)
{reject_section}
Rules:
- Make real, meaningful changes — not cosmetic tweaks.
- Only edit files in {TEMP_DIR.as_posix()}/ — never touch {ORIGIN_DIR.as_posix()}/ or root files.
- After editing, briefly summarize what you changed and why (plain text, no JSON).
- If WebSearch finds nothing useful for a topic, skip it and focus on what you can improve from code analysis alone."""


def run_improve_cycle(cycle: int) -> bool:
    """Run one improvement cycle using the local Ollama model. Returns True if something was changed."""
    print(f"\n[Cycle {cycle}] Local model researching and improving...")

    _sync_temp_from_origin()

    prompt = _build_improve_prompt(cycle)

    env = {"OCLI_EVAL": "1", **__import__("os").environ}
    stdin_text = prompt + "\nexit\n"

    try:
        result = subprocess.run(
            [sys.executable, "main.py"],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=600,
            env=env,
        )
        out = result.stdout.decode("utf-8", errors="replace")

        import re
        chunks = re.findall(r"MODEL:\n\n?(.*?)(?:\n\n>|\Z)", out, re.DOTALL)
        summary = chunks[-1].strip()[:400] if chunks else out[-400:].strip()
        print(f"  Model summary: {summary}")
        return True
    except subprocess.TimeoutExpired:
        print("  [improve timeout]")
        return False
    except Exception as e:
        print(f"  [improve error] {e}")
        return False


# ── compare step ────────────────────────────────────────────────────────────

def run_compare() -> str:
    """Run compare.py code --claude-review, return 'APPROVE'/'REJECT'/'ERROR'."""
    print("\n[Testing...]\n")
    out, err, rc = _run(
        [sys.executable, "compare.py", "code", "--claude-review"],
        timeout=1200,
    )
    print(out)

    # verdict comes from compare.py's history output
    history = _load_recent_history(1)
    if history:
        return history[0].get("verdict", "ERROR")
    return "ERROR"


# ── main loop ───────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 56)
    print("  OCLI Autonomous Improvement Loop")
    print(f"  max={MAX_CYCLES} cycles | "
          f"converge={APPROVE_STREAK} APPROVEs | "
          f"give-up={REJECT_STREAK} REJECTs")
    print("=" * 56)

    if not (ORIGIN_DIR / "main.py").exists():
        print("[ERROR] Origin/ not found. Run: python compare.py baseline")
        sys.exit(1)

    approve_streak = 0
    reject_streak  = 0

    for cycle in range(1, MAX_CYCLES + 1):
        print(f"\n{'─' * 56}")
        print(f"  CYCLE {cycle}/{MAX_CYCLES}")
        print(f"{'─' * 56}")

        changed = run_improve_cycle(cycle)
        if not changed:
            reject_streak += 1
            approve_streak = 0
            print(f"  Improvement failed  (reject streak={reject_streak})")
        else:
            verdict = run_compare()
            print(f"\n  Verdict: {verdict}")

            if verdict == "APPROVE":
                approve_streak += 1
                reject_streak  = 0
                print(f"  Promoting improvement (approve streak={approve_streak})")
                _promote()
                _update_baseline()

                if approve_streak >= APPROVE_STREAK:
                    print("\n" + "=" * 56)
                    print(f"  CONVERGED after {cycle} cycles.")
                    print("  Performance plateau reached — loop complete.")
                    print("=" * 56)
                    return

            else:  # REJECT or ERROR
                approve_streak = 0
                reject_streak += 1
                print(f"  Reverted (reject streak={reject_streak})")

        if reject_streak >= REJECT_STREAK:
            print("\n" + "=" * 56)
            print(f"  STUCK — {REJECT_STREAK} consecutive REJECTs.")
            print("  Cannot improve further with current approach.")
            print("=" * 56)
            return

    print("\n" + "=" * 56)
    print(f"  MAX CYCLES ({MAX_CYCLES}) reached.")
    print("=" * 56)


if __name__ == "__main__":
    main()
