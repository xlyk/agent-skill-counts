#!/usr/bin/env python3
"""Count recorded skill activations and SKILL.md read requests in local agent histories.

Python 3.10+, standard library only. No network or writes to harness data.
Usage: agent-skill-counts --out ./skill-usage-report
       agent-skill-counts --self-test

Counts are tool requests, not proof of successful execution or applying instructions.
Each skill is counted once per tool call; repeated IDs in copied histories are deduped.
System prompts, skill catalogs, user text, search results, and tool outputs do not count.
"""

import argparse
import ast
import collections
import csv
import datetime
import contextlib
import io
import json
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import tempfile


__version__ = "0.1.0"


# Canonical histories, including archives and subagents, rather than debug-log mirrors.
SOURCES = {
    "codex": [".codex/sessions", ".codex/archived_sessions"],
    "claude": [".claude/projects", ".claude/transcripts"],
    "pi": [".pi/agent/sessions"],
    "omp": [".omp/agent/sessions"],
    "grok": [".grok/sessions"],
    "hermes": [".hermes/state.db", ".hermes/profiles/*/state.db"],
    "cursor": [".cursor/projects/*/agent-transcripts", ".cursor/chats/*/*/store.db",
               "Library/Application Support/Cursor/User/globalStorage/state.vscdb"],
    "opencode": [".local/share/opencode/opencode.db", ".local/share/opencode/storage/message",
                 ".local/share/opencode/storage/part"],
    "copilot": ["Library/Application Support/Code/User/workspaceStorage/*/chatSessions",
                "Library/Application Support/Code/User/globalStorage/github.copilot-chat"],
    "cline": ["Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks"],
    "roo": ["Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/tasks"],
    "kilo": ["Library/Application Support/Code/User/globalStorage/kilocode.kilo-code/tasks"],
    "gemini": [".gemini/tmp/*/chats"],
    "amp": [".local/share/amp/threads", ".amp/threads"],
    "continue": [".continue/sessions"],
    "goose": [".local/share/goose/sessions"],
    "openclaw": [".openclaw/agents/*/sessions"],
    "superset": [".superset/local.db", ".superset/host/*/host.db"],
}
CALL_TYPES = {"tool_use", "toolCall", "tool-call", "function_call", "custom_tool_call"}
ACTIVATE = {"skill", "skill_view", "activate_skill", "load_skill", "use_skill"}
READ = {"read", "read_file", "read_file_v2", "readfile", "file_read", "read_text_file", "read_multiple_files"}
SHELL = {"bash", "shell", "terminal", "exec", "exec_command", "execute", "run_shell_command",
         "run_terminal_cmd", "run_terminal_command_v2", "terminal_exec", "functions.exec", "functions.run"}
JS_STRING = r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)'
NESTED_CMD = re.compile(r"\bexec_command\s*\(\s*\{[^}]*?\bcmd\s*:\s*" + JS_STRING, re.S)


def decode(value):
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, UnicodeError):
            pass
    return value


def skill_name(value):
    """Collapse harness-specific category/plugin prefixes to the skill's short name."""
    return str(value).strip().strip('"\'').replace("\\", "/").rstrip("/").split("/")[-1].split(":")[-1]


def path_skill(value):
    if not isinstance(value, str):
        return None
    value = value.rstrip("/ ")
    if "/" not in value.replace("\\", "/") or re.search(r"[$`{}*?%]", value):
        return None  # A bare filename or shell variable doesn't identify a skill.
    if value.replace("\\", "/").split("/")[-1] == "SKILL.md":
        return skill_name(value.replace("\\", "/").rsplit("/", 1)[0])
    return None


def shell_skills(command):
    # Do not interpret cat/read examples inside Python/JS heredoc bodies as commands.
    lines, delimiter = [], None
    for line in command.splitlines():
        if delimiter:
            if line.strip() == delimiter:
                delimiter = None
            continue
        lines.append(line)
        match = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if match:
            delimiter = match[1]
    try:
        lexer = shlex.shlex("\n".join(lines), posix=True, punctuation_chars=";&|\n<>")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return set()
    found, segment = set(), []
    for token in tokens + [";"]:
        if token and all(c in ";&|\n" for c in token):
            while segment and segment[0] in ("rtk", "proxy", "command", "sudo"):
                segment.pop(0)
            if segment and Path(segment[0]).name in {"cat", "head", "tail", "sed", "bat", "less", "more"}:
                for index, arg in enumerate(segment[1:], 1):
                    if segment[index - 1] in {">", ">>", ">&", ">|", "&>", "&>>"}:
                        continue
                    name = path_skill(arg)
                    if name:
                        found.add(name)
            segment = []
        else:
            segment.append(token)
    return found


def requests(node):
    """Yield only structured assistant tool requests, never recurse through prose/results."""
    if isinstance(node, list):
        for child in node:
            yield from requests(child)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "response_item":
        yield from requests(node.get("payload"))
        return
    if isinstance(kind, str) and (kind in CALL_TYPES or kind == "tool" and "state" in node):
        fn = node.get("function", node)
        if not isinstance(fn, dict):
            return
        state = node.get("state")
        state = state if isinstance(state, dict) else {}
        name = fn.get("name", node.get("toolName", node.get("tool", "")))
        args = fn.get("arguments", node.get("input", node.get("args", state.get("input", {}))))
        call_id = node.get("call_id", node.get("toolCallId", node.get("callID", node.get("id"))))
        yield name, decode(args), call_id
        return
    role = node.get("role", node.get("message", {}).get("role") if isinstance(node.get("message"), dict) else None)
    if role in ("user", "system", "developer", "tool") or kind in ("user", "system", "tool_result", "toolResult"):
        return
    if role == "assistant" or kind in ("assistant", "message"):
        message = node.get("message", node)
        if isinstance(message, dict):
            yield from requests(message.get("content"))
            for call in message.get("tool_calls", message.get("toolCalls", [])) or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function", call)
                if not isinstance(fn, dict):
                    continue
                yield fn.get("name", ""), decode(fn.get("arguments", fn.get("args", {}))), call.get("id", call.get("call_id"))
        return
    # Container keys seen in session exports; arguments/input/content are deliberately absent.
    for key in ("messages", "conversation", "history", "requests", "parts", "session"):
        if key in node:
            yield from requests(node[key])


def uses(tool, args):
    if not isinstance(tool, str):
        return set()
    leaf = tool.lower().split(".")[-1].split("__")[-1]
    if leaf in ACTIVATE and isinstance(args, dict):
        name = args.get("skill", args.get("name", args.get("skill_name")))
        if isinstance(name, str) and name.strip():
            return {(skill_name(name), "activations")}
    found = set()
    if leaf in READ and isinstance(args, dict):
        values = [args.get(k) for k in ("path", "file_path", "filePath", "target_file", "filename", "paths")]
        files = args.get("files")
        if isinstance(files, list):
            values += [a.get("path") for a in files if isinstance(a, dict)]
        for value in values:
            for path in value if isinstance(value, list) else [value]:
                name = path_skill(path)
                if name:
                    found.add((name, "file_reads"))
    if leaf in SHELL or tool.lower() in SHELL:
        commands = []
        if isinstance(args, dict):
            commands = [args[k] for k in ("cmd", "command", "script") if isinstance(args.get(k), str)]
            if isinstance(args.get("commands"), list):
                commands += [c for c in args["commands"] if isinstance(c, str)]
        elif isinstance(args, str):
            if leaf in ("exec", "run") and "exec_command" in args:
                quoted = [(m.start(), m.end()) for m in re.finditer(JS_STRING + r"|//[^\n]*|/\*.*?\*/", args, re.S)]
                for match in NESTED_CMD.finditer(args):
                    if any(start <= match.start() < end for start, end in quoted):
                        continue
                    literal = match[1]
                    try:
                        command = json.loads(literal) if literal.startswith('"') else ast.literal_eval(literal)
                    except (ValueError, SyntaxError):
                        continue
                    commands.append(command)
            else:
                commands = [args]
        for command in commands:
            found.update((name, "file_reads") for name in shell_skills(command))
    return found


def records(path, coverage, excluded_sessions=frozenset()):
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                # Large catalogs/prompts aren't decoded unless they contain a skill reference.
                coverage["lines"] += 1
                if "SKILL.md" not in line and not any(s in line for s in ('"Skill"', '"skill"', "skill_view", "activate_skill", "load_skill", "use_skill")):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    coverage["malformed_records"] += 1
    elif path.suffix == ".json":
        value = decode(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(value, (dict, list)):
            yield value
        else:
            coverage["malformed_records"] += 1
    elif path.suffix in (".db", ".sqlite", ".vscdb"):
        # mode=ro keeps committed WAL records visible. Never use immutable=1: it ignores WAL.
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {n for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" in tables and "tool_calls" in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                for (calls,) in db.execute("SELECT tool_calls FROM messages WHERE role='assistant' AND tool_calls IS NOT NULL"):
                    yield {"role": "assistant", "tool_calls": decode(calls)}
            elif "part" in tables:
                for (data,) in db.execute("SELECT data FROM part"):
                    yield decode(data)
            elif "cursorDiskKV" in tables:
                for key, data in db.execute("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
                    if key.split(":")[1] in excluded_sessions:
                        coverage["mirrored_records_skipped"] += 1
                        continue
                    bubble = decode(data)
                    if not isinstance(bubble, dict) or bubble.get("type") != 2:
                        continue
                    tool = bubble.get("toolFormerData")
                    if tool:
                        yield {"type": "function_call", "name": tool.get("name", ""),
                               "call_id": tool.get("toolCallId"),
                               "arguments": tool.get("rawArgs") or tool.get("params", {})}
            elif "blobs" in tables:
                for (blob,) in db.execute("SELECT data FROM blobs"):
                    value = decode(blob)
                    if isinstance(value, (dict, list)):
                        yield value
                    else:
                        coverage["non_json_blobs"] += 1
            else:
                coverage["unsupported_databases"] += 1


def discover(home, extra):
    sources = {key: list(paths) for key, paths in SOURCES.items()}
    for value in extra:
        harness, separator, path = value.partition("=")
        if not separator or not harness.strip() or not path:
            raise ValueError("--extra must be HARNESS=PATH")
        sources.setdefault(harness, []).append(path)
    for harness, patterns in sources.items():
        files, roots, text_only = set(), [], 0
        for pattern in patterns:
            p = Path(pattern).expanduser()
            matches = list(home.glob(pattern)) if not p.is_absolute() else [p]
            for root in matches:
                if not root.exists():
                    continue
                roots.append(str(root))
                candidates = root.rglob("*") if root.is_dir() else [root]
                for path in candidates:
                    if not path.is_file():
                        continue
                    if path.suffix == ".txt":
                        if "agent-transcripts" in path.parts:
                            text_only += 1
                    if path.suffix not in (".jsonl", ".json", ".db", ".sqlite", ".vscdb"):
                        continue
                    if harness == "grok" and path.name != "chat_history.jsonl":
                        continue
                    if harness in ("codex", "claude", "pi", "omp") and path.suffix != ".jsonl":
                        continue
                    if harness in ("cline", "roo", "kilo") and path.name != "api_conversation_history.json":
                        continue
                    files.add(path.resolve())
        yield harness, roots, sorted(files), text_only


def scan(home, extra=()):
    counts, seen, coverage = collections.defaultdict(collections.Counter), set(), {}
    for harness, roots, files, text_only in discover(home, extra):
        stats = collections.Counter(files=len(files), text_transcripts_not_parsed=text_only)
        stats["roots"] = roots
        coverage[harness] = stats
        print(f"Scanning {harness}: {len(files)} files", file=sys.stderr, flush=True)
        transcript_sessions = {p.stem for p in files if p.suffix == ".jsonl"} if harness == "cursor" else set()
        for path in files:
            if harness == "cursor" and path.name == "store.db" and path.parent.name in transcript_sessions:
                stats["mirrored_stores_skipped"] += 1
                continue
            try:
                for index, record in enumerate(records(path, stats, transcript_sessions)):
                    for call_index, (tool, args, call_id) in enumerate(requests(record)):
                        stats["tool_requests"] += 1
                        for name, kind in uses(tool, args):
                            # ponytail: ID-less Cursor copies share session and record positions;
                            # use native IDs if exports gain them or reorder their records.
                            source = path.stem if harness == "cursor" and path.suffix == ".jsonl" else str(path)
                            key = (harness, call_id or f"{source}:{index}:{call_index}", name, kind)
                            if key in seen:
                                stats["duplicate_skill_records"] += 1
                                continue
                            seen.add(key)
                            counts[(name, harness)][kind] += 1
                            stats["skill_requests"] += 1
                stats["files_scanned"] += 1
            except (OSError, sqlite3.Error, ValueError, TypeError) as error:
                stats["errors"] += 1
                stats.setdefault("error_files", []).append({"path": str(path), "error": type(error).__name__})
        stats["status"] = "not_found" if not roots else "partial" if stats["errors"] or stats["unsupported_databases"] or stats["malformed_records"] or text_only else "scanned"
        if harness == "superset" and stats["unsupported_databases"]:
            stats["note"] = "PTY wrapper: underlying harness histories are counted under their own names; no native message table found."
        elif roots and not stats["skill_requests"]:
            stats["note"] = "No skill-bearing tool records recognized; this is not proof that this harness never used skills."
    rows = [{"skill": name, "harness": harness, "activations": c["activations"], "file_reads": c["file_reads"], "total": sum(c.values())}
            for (name, harness), c in counts.items()]
    rows.sort(key=lambda r: (-r["total"], r["skill"], r["harness"]))
    totals = collections.Counter()
    for row in rows:
        totals[row["skill"]] += row["total"]
    return {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "home": str(home), "definition": "Recorded explicit activations + SKILL.md read requests, once per skill/tool call; not verified successful executions.",
            "limitations": ["Only local retained histories are available; deleted, remote, or unlogged activity is absent.",
                            "Skill names use the final category/plugin component. Catalogs, prose, tool results and search requests are excluded.",
                            "Shell reads recognize cat/head/tail/sed/bat/less/more with literal paths; dynamic scripts and variable paths can be missed.",
                            "Text-only transcripts and unknown SQLite schemas are reported as coverage gaps; non-JSON Cursor blobs are skipped and counted.",
                            "Cursor transcript exports take precedence over native stores for matching session IDs; ID-less exported copies are deduped by session and tool position."],
            "totals": dict(totals.most_common()), "by_harness": rows, "coverage": coverage}


def self_test():
    assert uses("Skill", {"skill": "ponytail:ponytail"}) == {("ponytail", "activations")}
    assert uses("skill_view", {"name": "development/tdd"}) == {("tdd", "activations")}
    assert uses("Read", {"path": "/a/skills/removed/SKILL.md"}) == {("removed", "file_reads")}
    assert shell_skills('rtk proxy cat "/a/skills/my skill/SKILL.md" && head /b/tdd/SKILL.md') == {"my skill", "tdd"}
    assert not shell_skills('rg SKILL.md /a/skills')
    assert not shell_skills('cat <<EOF > /a/new/SKILL.md\n# new skill\nEOF')
    assert shell_skills('cat /a/tdd/SKILL.md 2>/a/fake/SKILL.md') == {"tdd"}
    assert shell_skills('cat < /a/tdd/SKILL.md > /a/fake/SKILL.md') == {"tdd"}
    assert not shell_skills('cat "$d/SKILL.md" && cat SKILL.md && cat "/skills/${s}/SKILL.md"')
    assert not shell_skills('cat $s/SKILL.md && cat $W/SKILL.md')
    assert not shell_skills("python3 - <<'PY'\ncat /a/fake/SKILL.md\nPY")
    assert not list(requests({"role": "user", "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "fake"}}]}))
    assert not list(requests({"role": "tool", "content": [{"type": "tool_use", "name": "Skill"}]}))
    assert not uses(None, {"path": "/a/fake/SKILL.md"})
    assert not uses("Skill", {"skill": {"name": "fake"}})
    assert not list(requests({"type": "function_call", "function": None}))
    forms = [
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"cat /a/tdd/SKILL.md"}', "call_id": "a"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "Read", "input": {"path": "/a/tdd/SKILL.md"}}]}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "/a/tdd/SKILL.md"}}]}},
        {"role": "assistant", "tool_calls": [{"id": "d", "function": {"name": "skill_view", "arguments": '{"name":"tdd"}'}}]},
        {"role": "assistant", "content": [{"type": "tool-call", "toolName": "Read", "toolCallId": "e", "args": {"path": "/a/tdd/SKILL.md"}}]},
        {"type": "tool", "tool": "skill", "callID": "f", "state": {"input": {"name": "tdd"}}},
    ]
    for record in forms:
        calls = list(requests(record))
        assert len(calls) == 1 and {n for n, _ in uses(calls[0][0], calls[0][1])} == {"tdd"}, record
    assert uses("functions.exec", 'const r=await tools.exec_command({cmd:"rtk proxy cat /a/tdd/SKILL.md"});') == {("tdd", "file_reads")}
    assert not uses("functions.exec", 'const fixture="tools.exec_command({cmd:\'cat /a/fake/SKILL.md\'})";')
    assert not uses("functions.exec", '// tools.exec_command({cmd:"cat /a/fake/SKILL.md"});')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for relative in (".codex/sessions/a.jsonl", ".codex/archived_sessions/copy.jsonl"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(forms[0]) + "\n" + '{"incomplete":')
        path = root / ".claude/projects/test/session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"role": "assistant", "content": [{"type": "tool_use", "id": "skill-1", "name": "Skill", "input": {"skill": "tdd"}}]}) + "\n")
        path = root / ".hermes/state.db"
        path.parent.mkdir()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE messages(role TEXT, tool_calls TEXT)")
            db.execute("INSERT INTO messages VALUES ('assistant', ?)", (json.dumps(forms[3]["tool_calls"]),))
        cursor_record = {"role": "assistant", "content": [
            {"type": "tool-call", "toolName": "Read", "args": {"path": "/a/tdd/SKILL.md"}},
            {"type": "tool-call", "toolName": "Read", "args": {"path": "/a/tdd/SKILL.md"}}]}
        for project in ("demo", "copied-workspace"):
            path = root / f".cursor/projects/{project}/agent-transcripts/exported.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(cursor_record) + "\n")
        path = root / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT, value TEXT)")
            for session in ("exported", "native"):
                tool = {"name": "read_file_v2", "toolCallId": session,
                        "params": json.dumps({"path": "/a/tdd/SKILL.md"})}
                db.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                           (f"bubbleId:{session}:bubble", json.dumps({"type": 2, "toolFormerData": tool})))
        with contextlib.redirect_stderr(io.StringIO()):
            report = scan(root)
        assert report["totals"] == {"tdd": 6}, report["totals"]
        assert report["coverage"]["codex"]["duplicate_skill_records"] == 1
        assert report["coverage"]["cursor"]["duplicate_skill_records"] == 2
        assert report["coverage"]["cursor"]["mirrored_records_skipped"] == 1
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert main(["--home", str(root), "--out", str(root / "report")]) == 0
        saved = json.loads((root / "report/counts.json").read_text(encoding="utf-8"))
        assert saved["totals"] == {"tdd": 6}
        with (root / "report/totals.csv").open(encoding="utf-8", newline="") as fp:
            assert list(csv.DictReader(fp)) == [{"skill": "tdd", "recorded_uses": "6"}]
        with (root / "report/counts.csv").open(encoding="utf-8", newline="") as fp:
            assert sum(int(row["total"]) for row in csv.DictReader(fp)) == 6
        (root / ".codex/sessions/bad.jsonl").write_text('{"SKILL.md":', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert main(["--home", str(root), "--out", str(root / "report")]) == 1
        saved = json.loads((root / "report/counts.json").read_text(encoding="utf-8"))
        assert saved["coverage"]["codex"]["malformed_records"] == 1
        assert saved["totals"] == {"tdd": 6}
    print("PASS: native formats, code-mode reads, negative cases, JSONL/SQLite scan, deduplication and CLI reports")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory containing harness histories")
    parser.add_argument("--extra", action="append", default=[], metavar="HARNESS=PATH", help="Additional structured history file or directory; repeatable")
    parser.add_argument("--out", type=Path, help="Write counts.json, counts.csv and totals.csv here")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    home = args.home.expanduser().resolve()
    if not home.is_dir():
        parser.error("--home must be an existing directory")
    for value in args.extra:
        harness, separator, path = value.partition("=")
        if not separator or not harness.strip() or not path:
            parser.error("--extra must be HARNESS=PATH with a nonempty harness and path")
    report = scan(home, args.extra)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "counts.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        with (args.out / "counts.csv").open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=["skill", "harness", "activations", "file_reads", "total"])
            writer.writeheader()
            writer.writerows(report["by_harness"])
        with (args.out / "totals.csv").open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(["skill", "recorded_uses"])
            writer.writerows(report["totals"].items())
    print(f"{'Recorded uses':>14}  Skill")
    for name, total in report["totals"].items():
        print(f"{total:14,d}  {name}")
    print("\nCoverage (see counts.json for details):")
    for harness, stats in report["coverage"].items():
        print(f"  {harness:10} {stats['status']:10} files={stats['files_scanned']} calls={stats['skill_requests']} errors={stats['errors']}")
    return int(any(s["errors"] or s["malformed_records"] for s in report["coverage"].values()))


if __name__ == "__main__":
    sys.exit(main())
