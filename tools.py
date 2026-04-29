# tools.py

import os
import re
import json
import math
import glob
import difflib
import subprocess
import requests
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from ddgs import DDGS


RUN_TIMEOUT = 30
FETCH_TIMEOUT = 20

_LIST_DIR_SKIP = {
    ".git", ".svn", ".hg",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", ".DS_Store"
}

TOOL_ALIAS = {
    "LIST_FILES": "LIST_DIR",
    "LISTDIR": "LIST_DIR",
    "ANALYZE_WORKSPACE": "LIST_DIR",

    "READFILE": "READ_FILE",
    "READ_FILE": "READ_FILE",

    "WRITEFILE": "WRITE_FILE",
    "WRITE_FILE": "WRITE_FILE",

    "FETCHURL": "FETCH_URL",
    "FETCH_URL": "FETCH_URL",

    "CALC": "CALCULATE",
    "MATH": "CALCULATE",

    "SEARCH_WEB": "SEARCH",
    "GOOGLE_SEARCH": "SEARCH",

    "GREP": "GREP",
    "SEARCH_FILE": "GREP",
    "FIND_IN_FILE": "GREP",

    "EDIT_FILE": "EDIT_FILE",
    "REPLACE": "EDIT_FILE",
    "MODIFY_FILE": "EDIT_FILE",
}


class ToolRouter:
    def __init__(self):
        pass

    def run(self, response):
        response = response.strip()

        if "<tool_call>" in response:
            blocks = re.findall(r"<tool_call>.*?</tool_call>", response, re.DOTALL)

            if len(blocks) > 1:
                results = []
                for block in blocks:
                    result = self.handle_xml_tool_call(block)
                    results.append(result)

                all_success = all(r.get("success", False) for r in results)
                combined_summary = "\n\n---\n\n".join(
                    f"[{r.get('tool', 'unknown')}]\n{r.get('summary', '')}"
                    for r in results
                )
                return {
                    "tool": "multi",
                    "success": all_success,
                    "summary": combined_summary[:3000],
                    "content": results
                }

            return self.handle_xml_tool_call(response)

        bash_match = re.search(
            r"```bash\s*(.*?)```",
            response,
            re.DOTALL
        )

        if bash_match:
            command = bash_match.group(1).strip()
            return self.safe_run(command)

        return None

    def handle_xml_tool_call(self, response):
        try:
            # STEP 1: strict XML
            xml_match = re.search(
                r"(<tool_call>.*?</tool_call>)",
                response,
                re.DOTALL
            )

            if xml_match:
                xml_block = xml_match.group(1)

                try:
                    root = ET.fromstring(xml_block)
                    tool_name_node = root.find("tool_name")

                    if tool_name_node is not None and tool_name_node.text:
                        tool_name = TOOL_ALIAS.get(
                            tool_name_node.text.strip().upper(),
                            tool_name_node.text.strip().upper()
                        )

                        args_node = (
                            root.find("tool_args")
                            or root.find("tool_params")
                        )

                        args = {}
                        if args_node is not None:
                            for child in args_node:
                                key = child.tag.lower()
                                value = child.text if child.text is not None else ""
                                # content/text/old_text/new_text preserve whitespace; others strip
                                args[key] = value if key in ("content", "text", "old_text", "new_text") else value.strip()

                        print(f"\n[XML TOOL] {tool_name}")
                        print(f"[XML ARGS] {args}\n")

                        return self.route_tool(tool_name, args, response)

                except Exception as strict_error:
                    print(f"[STRICT XML FAILED] {strict_error}")

            # STEP 2: reject malformed XML — model must use strict format
            return {
                "tool": "xml_parser",
                "success": False,
                "summary": "Malformed tool_call XML. Use strict format: <tool_call><tool_name>NAME</tool_name><tool_args><arg>value</arg></tool_args></tool_call>",
                "content": response
            }

        except Exception as e:
            return {
                "tool": "xml_parser",
                "success": False,
                "summary": f"XML parse failed: {e}",
                "content": response
            }

    def route_tool(self, tool_name, args, raw):
        if tool_name == "SEARCH":
            return self.search(args.get("query", ""))

        if tool_name == "CALCULATE":
            expression = (
                args.get("expression")
                or args.get("expr")
                or args.get("formula")
                or args.get("path")
                or ""
            )
            return self.calculate(expression)

        if tool_name == "FETCH_URL":
            return self.fetch_url(args.get("url", ""))

        if tool_name == "READ_FILE":
            path = (
                args.get("path")
                or args.get("file")
                or args.get("filepath")
                or ""
            )
            start = args.get("start_line") or args.get("start") or args.get("from_line")
            end = args.get("end_line") or args.get("end") or args.get("to_line")
            return self.read_file(path, start, end)

        if tool_name == "LIST_DIR":
            path = (
                args.get("path")
                or args.get("root")
                or args.get("directory")
                or "."
            )
            return self.list_dir(path)

        if tool_name == "GLOB":
            pattern = (
                args.get("pattern")
                or args.get("glob")
                or args.get("query")
                or ""
            )
            return self.glob_files(pattern)

        if tool_name == "WRITE_FILE":
            path = args.get("path") or args.get("file") or ""
            content = args.get("content") or args.get("text") or ""
            return self.write_file(path, content)

        if tool_name == "GREP":
            path = args.get("path") or args.get("file") or ""
            pattern = args.get("pattern") or args.get("query") or args.get("text") or ""
            context = args.get("context_lines") or args.get("context") or "0"
            return self.grep_file(path, pattern, context)

        if tool_name == "EDIT_FILE":
            path = args.get("path") or args.get("file") or ""
            old_text = args.get("old_text") or args.get("old") or args.get("find") or ""
            new_text = args.get("new_text", args.get("new", args.get("replace", "")))
            return self.edit_file(path, old_text, new_text)

        if tool_name == "RUN":
            command = (
                args.get("command")
                or args.get("cmd")
                or args.get("path")
                or ""
            )
            return self.safe_run(command)

        return {
            "tool": "unknown",
            "success": False,
            "summary": f"Unknown tool: {tool_name}",
            "content": raw
        }

    def search(self, query):
        try:
            results = list(DDGS().text(query, max_results=8))
            if not results:
                return {
                    "tool": "search",
                    "success": False,
                    "summary": "No results found",
                    "content": [],
                }

            summary = "\n\n".join(
                f"{r['title']}\n{r['href']}\n{r['body']}"
                for r in results
            )
            return {
                "tool": "search",
                "success": True,
                "summary": summary[:3000],
                "content": results,
            }

        except Exception as e:
            return {
                "tool": "search",
                "success": False,
                "summary": f"Search failed: {e}",
                "content": str(e),
            }

    def calculate(self, expression):
        try:
            allowed_names = {
                k: getattr(math, k)
                for k in dir(math)
                if not k.startswith("_")
            }

            allowed_names.update({
                "abs": abs,
                "round": round,
                "min": min,
                "max": max
            })

            code = compile(expression, "<string>", "eval")

            for name in code.co_names:
                if name not in allowed_names:
                    raise ValueError(f"Use of '{name}' not allowed")

            result = eval(
                code,
                {"__builtins__": {}},
                allowed_names
            )

            return {
                "tool": "calculate",
                "success": True,
                "summary": f"Result: {result}",
                "content": {
                    "expression": expression,
                    "result": result
                }
            }

        except Exception as e:
            return {
                "tool": "calculate",
                "success": False,
                "summary": f"Calculation failed: {e}",
                "content": str(e)
            }

    def fetch_url(self, url):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}

            r = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")

            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)[:8000]

            return {
                "tool": "fetch_url",
                "success": True,
                "summary": text[:3000],
                "content": text
            }

        except Exception as e:
            return {
                "tool": "fetch_url",
                "success": False,
                "summary": f"Fetch failed: {e}",
                "content": str(e)
            }

    def read_file(self, path, start_line=None, end_line=None):
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(path)
            if not (target == cwd or target.startswith(cwd + os.sep)):
                return {
                    "tool": "read_file",
                    "success": False,
                    "summary": f"Read outside working directory denied: {path}",
                    "content": path
                }
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total = len(lines)

            start = max(0, int(start_line) - 1) if start_line else 0
            end = min(total, int(end_line)) if end_line else total

            selected = lines[start:end]

            text = "".join(
                f"{start + i + 1}: {line}"
                for i, line in enumerate(selected)
            )

            truncated = False
            if len(text) > 8000:
                text = text[:8000]
                last_newline = text.rfind("\n")
                if last_newline > 0:
                    text = text[:last_newline + 1]
                actual_end = start + text.count("\n")
                truncated = True
            else:
                actual_end = start + len(selected)

            if truncated:
                meta = (
                    f"[Lines {start + 1}-{actual_end} / {total}]"
                    f" [TRUNCATED -- use start_line/end_line to read more]\n"
                )
            else:
                meta = f"[Lines {start + 1}-{actual_end} / {total}]\n"

            return {
                "tool": "read_file",
                "success": True,
                "summary": (meta + text)[:3000],
                "content": meta + text
            }

        except Exception as e:
            return {
                "tool": "read_file",
                "success": False,
                "summary": f"Read failed: {e}",
                "content": str(e)
            }

    def list_dir(self, path):
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(path if path else ".")

            if not (target == cwd or target.startswith(cwd + os.sep)):
                return {
                    "tool": "list_dir",
                    "success": False,
                    "summary": f"Listing outside working directory denied: {path}",
                    "content": path
                }

            if not os.path.exists(target):
                return {
                    "tool": "list_dir",
                    "success": False,
                    "summary": f"Path not found: {path}",
                    "content": path
                }

            items = []

            for name in sorted(os.listdir(target)):
                if name in _LIST_DIR_SKIP:
                    continue
                full = os.path.join(target, name)
                prefix = "[DIR]" if os.path.isdir(full) else "[FILE]"
                items.append(f"{prefix} {name}")

            text = "\n".join(items[:500])

            return {
                "tool": "list_dir",
                "success": True,
                "summary": text[:3000],
                "content": text
            }

        except Exception as e:
            return {
                "tool": "list_dir",
                "success": False,
                "summary": f"LIST_DIR failed: {e}",
                "content": str(e)
            }

    def glob_files(self, pattern):
        try:
            files = glob.glob(pattern, recursive=True)
            cwd = os.path.abspath(os.getcwd())
            files = [
                f for f in files
                if os.path.abspath(f) == cwd or os.path.abspath(f).startswith(cwd + os.sep)
            ]

            if not files:
                return {
                    "tool": "glob",
                    "success": True,
                    "summary": "No files matched",
                    "content": []
                }

            text = "\n".join(files[:1000])

            return {
                "tool": "glob",
                "success": True,
                "summary": text[:3000],
                "content": files[:1000]
            }

        except Exception as e:
            return {
                "tool": "glob",
                "success": False,
                "summary": f"GLOB failed: {e}",
                "content": str(e)
            }

    def grep_file(self, path, pattern, context_lines=0):
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(path)
            if not (target == cwd or target.startswith(cwd + os.sep)):
                return {
                    "tool": "grep",
                    "success": False,
                    "summary": f"Read outside working directory denied: {path}",
                    "content": path
                }
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()

            ctx = max(0, int(context_lines)) if context_lines else 0
            compiled = re.compile(pattern)

            match_indices = [i for i, line in enumerate(lines) if compiled.search(line)]

            if not match_indices:
                return {
                    "tool": "grep",
                    "success": True,
                    "summary": f"No matches for pattern: {pattern}",
                    "content": []
                }

            # Merge overlapping context ranges to avoid duplicate lines
            ranges = []
            for i in match_indices:
                start = max(0, i - ctx)
                end = min(len(lines), i + ctx + 1)
                if ranges and start <= ranges[-1][1]:
                    prev = ranges[-1]
                    ranges[-1] = (prev[0], max(end, prev[1]), prev[2] | {i})
                else:
                    ranges.append((start, end, {i}))

            matches = []
            for start, end, match_set in ranges:
                block = []
                for j in range(start, end):
                    prefix = ">" if j in match_set else " "
                    block.append(f"{prefix} {j + 1}: {lines[j].rstrip()}")
                matches.append("\n".join(block))

            text = "\n---\n".join(matches)[:8000]

            return {
                "tool": "grep",
                "success": True,
                "summary": text[:3000],
                "content": matches[:500]
            }

        except Exception as e:
            return {
                "tool": "grep",
                "success": False,
                "summary": f"GREP failed: {e}",
                "content": str(e)
            }

    def edit_file(self, path, old_text, new_text):
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(path)

            if not (target == cwd or target.startswith(cwd + os.sep)):
                return {
                    "tool": "edit_file",
                    "success": False,
                    "summary": f"Edit outside working directory denied: {path}",
                    "content": path
                }

            # Unescape \n and \t that models write as escape sequences
            old_text = old_text.replace("\\n", "\n").replace("\\t", "\t")
            new_text = new_text.replace("\\n", "\n").replace("\\t", "\t")

            with open(target, "r", encoding="utf-8") as f:
                original = f.read()

            if old_text not in original:
                return {
                    "tool": "edit_file",
                    "success": False,
                    "summary": f"old_text not found in {path}",
                    "content": {"path": path, "old_text": old_text}
                }

            count = original.count(old_text)
            if count > 1:
                return {
                    "tool": "edit_file",
                    "success": False,
                    "summary": f"{count} occurrences of old_text found in {path}. Provide a more specific old_text that matches exactly once.",
                    "content": {"path": path, "count": count}
                }

            new_content = original.replace(old_text, new_text, 1)

            old_lines = original.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm=""
            ))
            diff_text = "\n".join(diff_lines) if diff_lines else "[NO CHANGES]"

            preview = diff_text[:2000]
            if len(diff_text) > 2000:
                preview += "\n... [diff truncated]"
            print(f"\nPreview:\n{preview}\n")

            if os.environ.get("OCLI_EVAL") != "1":
                allow = input(
                    f"\nAllow file edit? [y/n]\n{path}\n> "
                ).strip().lower()
                if allow != "y":
                    return {
                        "tool": "edit_file",
                        "success": False,
                        "summary": "Edit denied by user",
                        "content": path
                    }

            with open(target, "w", encoding="utf-8") as f:
                f.write(new_content)

            summary = f"File edited: {path}\n\n{diff_text}"

            return {
                "tool": "edit_file",
                "success": True,
                "summary": summary[:3000],
                "content": {
                    "path": path,
                    "diff": diff_text[:8000]
                }
            }

        except FileNotFoundError:
            return {
                "tool": "edit_file",
                "success": False,
                "summary": f"File not found: {path}",
                "content": path
            }
        except Exception as e:
            return {
                "tool": "edit_file",
                "success": False,
                "summary": f"EDIT_FILE failed: {e}",
                "content": str(e)
            }

    def write_file(self, path, content):
        try:
            cwd = os.path.abspath(os.getcwd())
            target = os.path.abspath(path)

            if not (target == cwd or target.startswith(cwd + os.sep)):
                return {
                    "tool": "write_file",
                    "success": False,
                    "summary": f"Write outside working directory denied: {path}",
                    "content": path
                }

            # Read existing content for diff (if file exists)
            old_lines = []
            is_new_file = not os.path.exists(target)
            if not is_new_file:
                try:
                    with open(target, "r", encoding="utf-8") as f:
                        old_lines = f.readlines()
                except Exception:
                    is_new_file = True

            new_lines = content.splitlines(keepends=True)

            if is_new_file:
                diff_text = f"[NEW FILE] {path}\n"
                diff_text += "".join(f"+ {l}" for l in new_lines)
            else:
                diff_lines = list(difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm=""
                ))
                diff_text = "\n".join(diff_lines) if diff_lines else "[NO CHANGES]"

            preview = diff_text[:2000]
            if len(diff_text) > 2000:
                preview += "\n... [diff truncated]"
            print(f"\nPreview:\n{preview}\n")

            if os.environ.get("OCLI_EVAL") != "1":
                allow = input(
                    f"\nAllow file write? [y/n]\n{path}\n> "
                ).strip().lower()
                if allow != "y":
                    return {
                        "tool": "write_file",
                        "success": False,
                        "summary": "Write denied by user",
                        "content": path
                    }

            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)

            with open(target, "w", encoding="utf-8") as f:
                f.write(content)

            summary = f"File written: {path}\n\n{diff_text}"

            return {
                "tool": "write_file",
                "success": True,
                "summary": summary[:3000],
                "content": {
                    "path": path,
                    "diff": diff_text[:8000]
                }
            }

        except Exception as e:
            return {
                "tool": "write_file",
                "success": False,
                "summary": f"WRITE_FILE failed: {e}",
                "content": str(e)
            }

    def safe_run(self, command):
        _BLOCKED = re.compile(
            r"\b(rm|del|rmdir|rd|format|shutdown|reboot|diskpart|mkfs|dd|taskkill)\b",
            re.IGNORECASE
        )

        if _BLOCKED.search(command):
            return {
                "tool": "run",
                "success": False,
                "summary": "Blocked unsafe command",
                "content": command
            }

        allow = input(
            f"\nAllow command? [y/n]\n{command}\n> "
        ).strip().lower()

        if allow != "y":
            return {
                "tool": "run",
                "success": False,
                "summary": "Command denied by user",
                "content": command
            }

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT
            )

            output = (
                f"Exit code: {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
            )[:8000]

            return {
                "tool": "run",
                "success": result.returncode == 0,
                "summary": output[:3000],
                "content": output
            }

        except Exception as e:
            return {
                "tool": "run",
                "success": False,
                "summary": f"Run failed: {e}",
                "content": str(e)
            }
