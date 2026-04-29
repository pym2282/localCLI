import re
import sys
import json
import hashlib
import requests

from tools import ToolRouter


if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stdin.encoding and sys.stdin.encoding.lower() != 'utf-8':
    sys.stdin.reconfigure(encoding='utf-8')


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3-14b-q4km"

MAX_TOOL_LOOPS = 15
MAX_CONTINUE = 3

# stronger loop detection
TOOL_PATTERN_WINDOW = 8
TOOL_REPEAT_THRESHOLD = 3

# context management
MAX_CONTEXT_CHARS = 35000
SUMMARY_KEEP_RECENT = 6

# timeouts
OLLAMA_TIMEOUT_STREAM = (10, 300)
OLLAMA_TIMEOUT_SUMMARY = (10, 120)

GRAY = "\033[90m"
RESET = "\033[0m"

SYSTEM_PROMPT_BASE = """
You are OCLI, a local coding and research assistant.
Your full instructions are provided below from OCLI.md.
Follow them exactly.
"""


def load_system_prompt():
    try:
        with open("OCLI.md", "r", encoding="utf-8") as f:
            ocli = f.read()
        return SYSTEM_PROMPT_BASE.strip() + "\n\n" + ocli
    except FileNotFoundError:
        print("[WARNING] OCLI.md not found. Running with minimal prompt.")
        return SYSTEM_PROMPT_BASE.strip()


def extract_think(text):
    return re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL,
    ).strip()


def extract_tool_signatures(response):
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", response, re.DOTALL)
    return [re.sub(r"\s+", " ", b.strip()) for b in blocks]


def hash_signature(sig):
    return hashlib.md5(sig.encode("utf-8")).hexdigest()


def is_tool_loop(sig_history, current_sigs):
    if not current_sigs:
        return False

    current_hashes = [hash_signature(x) for x in current_sigs]
    combined = "|".join(current_hashes)
    sig_history.append(combined)

    if len(sig_history) < TOOL_PATTERN_WINDOW:
        return False

    recent = sig_history[-TOOL_PATTERN_WINDOW:]

    if len(set(recent[-TOOL_REPEAT_THRESHOLD:])) == 1:
        return True

    if len(recent) >= 6:
        a, b = recent[-2], recent[-1]
        if recent[-6:] == [a, b, a, b, a, b]:
            return True

    return False


def should_summarize(messages):
    total_chars = sum(len(m.get("content", "")) for m in messages)
    return total_chars > MAX_CONTEXT_CHARS


def summarize_history(messages):
    old_messages = messages[1:-SUMMARY_KEEP_RECENT]

    if not old_messages:
        return None

    user_goals = []
    tool_results = []
    assistant_reasoning = []
    confirmed_state = []

    seen_chunks = set()

    def clean(text, limit=None):
        if not text:
            return ""
        text = text.strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        if limit and len(text) > limit:
            text = text[:limit] + "\n... [truncated]"
        return text

    def dedupe_push(target, text):
        text = clean(text)
        if not text:
            return
        sig = hashlib.md5(text.encode("utf-8")).hexdigest()
        if sig in seen_chunks:
            return
        seen_chunks.add(sig)
        target.append(text)

    for msg in old_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if not content:
            continue

        content = clean(content)

        if content.startswith("Tool result:"):
            try:
                json_part = content.split("Tool result:", 1)[1].strip()
                match = re.search(r"\{.*\}", json_part, re.DOTALL)

                if match:
                    parsed = json.loads(match.group())
                    tool_name = parsed.get("tool", "unknown")
                    success = parsed.get("success", False)
                    summary = parsed.get("summary", "")
                    important = parsed.get("important_lines", "")

                    compact = f"[{tool_name}] success={success}\n{summary}"
                    if important:
                        compact += f"\nimportant_lines: {important}"
                    dedupe_push(tool_results, compact)
                else:
                    dedupe_push(tool_results, content[:800])
            except Exception:
                dedupe_push(tool_results, content[:800])
            continue

        if role == "user":
            dedupe_push(user_goals, clean(content, 1000))
            continue

        if role == "system":
            if "Do not repeat the same tool pattern" in content:
                dedupe_push(
                    confirmed_state,
                    "Repeated tool loop previously detected. Reuse existing tool results before calling tools."
                )
                continue
            if "Tool limit reached" in content:
                dedupe_push(
                    confirmed_state,
                    "Tool limit was previously reached. Prefer finalization over more tool calls."
                )
                continue
            if "SESSION MEMORY:" in content:
                dedupe_push(
                    confirmed_state,
                    clean(content.replace("SESSION MEMORY:", ""), 1200)
                )
                continue
            continue

        if role == "assistant":
            if "<tool_call>" in content:
                continue
            dedupe_push(assistant_reasoning, clean(content, 1200))
            continue

    structured_context = f"""
USER GOALS
{chr(10).join("- " + x for x in user_goals[-12:])}

CONFIRMED STATE
{chr(10).join("- " + x for x in confirmed_state[-12:])}

TOOL RESULTS
{chr(10).join("- " + x for x in tool_results[-20:])}

ASSISTANT REASONING
{chr(10).join("- " + x for x in assistant_reasoning[-12:])}
"""

    summary_prompt = f"""
Create a concise engineering working-memory snapshot.

Return ONLY this exact tagged plaintext format:

[PROJECT_STATE]
...

[CONFIRMED_DECISIONS]
...

[REJECTED_APPROACHES]
...

[ROOT_CAUSES]
...

[CONSTRAINTS]
...

[FILES]
...

[NEXT_ACTIONS]
...

STRICT RULES:
- preserve exact bug root causes
- preserve exact failures
- preserve exact rejected approaches
- preserve exact filenames
- preserve exact TODOs
- preserve exact constraints
- preserve exact debugging conclusions
- preserve parity failures if mentioned
- preserve replication bugs if mentioned
- remove repeated chat noise
- remove tool spam
- remove conversational filler
- concise but lossless
- this is WORKING MEMORY, not a chat summary
- optimize for future coding continuation

Context:

{structured_context}
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert coding-agent memory compressor. "
                    "Preserve implementation continuity with minimal tokens."
                ),
            },
            {
                "role": "user",
                "content": summary_prompt,
            },
        ],
        "stream": False,
    }

    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT_SUMMARY)
        r.raise_for_status()

        data = r.json()
        summary = data.get("message", {}).get("content", "").strip()

        if not summary:
            return None

        summary = summary.strip()
        summary = re.sub(r"^```(?:text|txt)?", "", summary).strip()
        summary = re.sub(r"```$", "", summary).strip()

        return summary

    except Exception as e:
        print(f"[summary failed] {e}")
        return None


def compact_messages(messages):
    if not should_summarize(messages):
        return messages

    summary = summarize_history(messages)

    if not summary:
        return messages

    print("\n[Session summarized]\n")

    keep_count = SUMMARY_KEEP_RECENT
    while keep_count < len(messages) - 1:
        candidate = messages[-keep_count]
        if candidate["role"] == "user":
            break
        keep_count += 1

    return [
        messages[0],
        {
            "role": "system",
            "content": "SESSION MEMORY:\n\n" + summary,
        },
        *messages[-keep_count:],
    ]


def call_ollama(messages):
    """Returns (response_text, partial_text). partial_text is set only on __CONTINUE__."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
    }

    try:
        r = requests.post(
            OLLAMA_URL,
            json=payload,
            stream=True,
            timeout=OLLAMA_TIMEOUT_STREAM,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"\n[Connection error] {e}\n")
        return None, None

    full_text = ""
    buffer = ""
    inside_think = False
    got_done_signal = False

    print("\nMODEL:\n")

    try:
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue

            try:
                data = json.loads(line)
            except Exception:
                continue

            if data.get("done", False):
                got_done_signal = True
                break

            chunk = data.get("message", {}).get("content", "")
            if not chunk:
                continue

            full_text += chunk
            buffer += chunk

            while buffer:
                if not inside_think:
                    pos = buffer.find("<think>")

                    if pos == -1:
                        print(buffer, end="", flush=True)
                        buffer = ""
                        break

                    visible = buffer[:pos]
                    if visible:
                        print(visible, end="", flush=True)

                    buffer = buffer[pos + len("<think>"):]
                    inside_think = True
                    print(GRAY, end="", flush=True)

                else:
                    pos = buffer.find("</think>")

                    if pos == -1:
                        hold = len("</think>") - 1
                        if len(buffer) > hold:
                            print(buffer[:-hold], end="", flush=True)
                            buffer = buffer[-hold:]
                        break

                    think_chunk = buffer[:pos]
                    if think_chunk:
                        print(think_chunk, end="", flush=True)

                    buffer = buffer[pos + len("</think>"):]
                    inside_think = False
                    print(RESET + "\n", end="", flush=True)

    except Exception as e:
        print(f"\n[Stream error] {e}\n")

    print("\n")

    if not got_done_signal:
        print("[WARNING] Response may be incomplete.")
        return "__CONTINUE__", full_text

    return extract_think(full_text), None


def build_tool_memory(tool_result):
    if tool_result.get("tool") == "multi":
        sub_results = tool_result.get("content", [])
        parts = []
        for r in sub_results:
            entry = {
                "tool": r.get("tool"),
                "success": r.get("success"),
                "summary": r.get("summary", ""),
                "next_recommended_action": (
                    "Use existing file info before re-reading."
                    if r.get("tool") == "read_file"
                    else "Use existing results before calling more tools."
                ),
            }
            if r.get("tool") == "read_file":
                entry["important_lines"] = r.get("range", "")
            parts.append(entry)
        return {
            "tool": "multi",
            "success": tool_result.get("success"),
            "results": parts,
        }

    if tool_result.get("tool") == "read_file":
        return {
            "tool": "read_file",
            "success": tool_result.get("success"),
            "summary": tool_result.get("summary", ""),
            "important_lines": tool_result.get("range", ""),
            "next_recommended_action": "Use existing file info before re-reading.",
        }

    return {
        "tool": tool_result.get("tool"),
        "success": tool_result.get("success"),
        "summary": tool_result.get("summary", ""),
        "next_recommended_action": "Use existing results before calling more tools.",
    }


def main():
    print(
        "Local Agent CLI started. Type 'exit' to quit.\n"
        "Commands: /reset | /status\n"
    )

    tool_router = ToolRouter()

    messages = [
        {
            "role": "system",
            "content": load_system_prompt(),
        }
    ]

    while True:
        user_input = input("> ").strip()

        if user_input.lower() in ["exit", "quit"]:
            print("Goodbye.")
            break

        if user_input.lower() == "/reset":
            messages = [messages[0]]
            print("[Conversation reset]\n")
            continue

        if user_input.lower() == "/status":
            total_chars = sum(len(m.get("content", "")) for m in messages)
            print(
                f"[Session: {len(messages)} messages | ~{total_chars} chars | summary at {MAX_CONTEXT_CHARS}]\n"
            )
            continue

        if not user_input:
            continue

        messages.append({
            "role": "user",
            "content": user_input,
        })

        messages = compact_messages(messages)
        tool_sig_history = []

        for _ in range(MAX_TOOL_LOOPS):
            response, last_partial = call_ollama(messages)

            if response is None:
                break

            continue_count = 0
            accumulated_partial = ""

            while response == "__CONTINUE__" and continue_count < MAX_CONTINUE:
                if last_partial:
                    accumulated_partial += extract_think(last_partial)

                anchor_prompt = (
                    "You stopped mid-generation after this exact text:\n\n"
                    f"{accumulated_partial[-2000:]}\n\n"
                    "Continue exactly from there. Do not restart. Do not repeat. Finalize only."
                )

                temp_messages = messages + [
                    {
                        "role": "user",
                        "content": anchor_prompt,
                    }
                ]

                response, last_partial = call_ollama(temp_messages)
                continue_count += 1

            if response and response != "__CONTINUE__" and accumulated_partial:
                response = accumulated_partial + response

            if response is None or response == "__CONTINUE__":
                break

            current_sigs = extract_tool_signatures(response)

            if is_tool_loop(tool_sig_history, current_sigs):
                print("\n[Tool loop detected]\n")
                messages.append({
                    "role": "system",
                    "content": (
                        "Do not repeat the same tool pattern. "
                        "Use existing results. Finalize now."
                    ),
                })
                continue

            tool_result = tool_router.run(response)

            if tool_result is None:
                messages.append({
                    "role": "assistant",
                    "content": response,
                })
                break

            print("\nTOOL RESULT:\n")
            print(tool_result.get("summary", ""))
            print()

            messages.append({
                "role": "assistant",
                "content": response,
            })

            slim = build_tool_memory(tool_result)

            messages.append({
                "role": "system",
                "content": (
                    "Tool result:\n\n"
                    + json.dumps(slim, ensure_ascii=False, indent=2)
                    + "\n\nUse this result. Avoid repeated tool calls. Finalize if enough information exists."
                ),
            })

            messages = compact_messages(messages)

        else:
            messages.append({
                "role": "system",
                "content": (
                    "Tool limit reached. Summarize findings and give final answer now."
                ),
            })

            response, _ = call_ollama(messages)
            if response:
                messages.append({
                    "role": "assistant",
                    "content": response,
                })


if __name__ == "__main__":
    main()
