# CLI reference

## Flags

| Flag | Behavior |
| --- | --- |
| `--out DIR` | Write `totals.csv`, `counts.csv`, and `counts.json`. Without it, print counts and a coverage summary only. Existing report files are replaced. |
| `--home DIR` | Select an existing directory containing harness histories. Defaults to the current user's home. |
| `--extra HARNESS=PATH` | Add a structured history file or directory. Repeatable. Relative paths are resolved under `--home`; `~` expands to the current user's home. |
| `--self-test` | Run the synthetic check without scanning your histories. |
| `--version` | Print the version. |
| `--help` | Print usage. |

Exit codes: `0` for a completed scan or successful self-test; `1` if a scan encounters unreadable files or malformed candidate records; `2` for invalid CLI arguments. Partial results remain available after scan errors. Missing roots and unsupported schemas are reported in coverage and do not cause a nonzero exit code. Unhandled output or discovery filesystem errors also terminate with a nonzero exit code.

## Source discovery

Paths are relative to `--home`. Directories are scanned recursively, including archives and subagent histories. These paths were inspected against retained histories on macOS; the tool does not promise every version of each harness uses this schema.

| Harness | Sources |
| --- | --- |
| Codex | `.codex/sessions`, `.codex/archived_sessions` (`*.jsonl`) |
| Claude Code | `.claude/projects`, `.claude/transcripts` (`*.jsonl`) |
| Pi | `.pi/agent/sessions` (`*.jsonl`) |
| Oh My Pi | `.omp/agent/sessions` (`*.jsonl`) |
| Grok | `.grok/sessions/**/chat_history.jsonl` |
| Hermes | `.hermes/state.db`, `.hermes/profiles/*/state.db` (`messages.tool_calls`) |
| Cursor | `.cursor/projects/*/agent-transcripts`, `.cursor/chats/*/*/store.db`, `Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| OpenCode | `.local/share/opencode/opencode.db`, `.local/share/opencode/storage/message`, `.local/share/opencode/storage/part` |

Additional candidate paths are searched for Copilot, Cline, Roo, Kilo, Gemini, Amp, Continue, Goose, OpenClaw, and Superset; they are listed in the module's `SOURCES` dictionary. Their native schemas have not been verified. IDE discovery paths currently use macOS locations; custom homes and `--extra` can supply equivalent structured logs elsewhere.

Superset is a PTY wrapper. Its discovered databases did not contain native assistant messages; underlying provider histories are counted under the provider's name.

## Count fields

| File | Content |
| --- | --- |
| `totals.csv` | `skill`, `recorded_uses`, sorted by descending count. |
| `counts.csv` | `skill`, `harness`, `activations`, `file_reads`, `total`. |
| `counts.json` | Generation time, selected home, count definition, limitations, totals, per-harness rows, and coverage statistics. |

Activations recognize `Skill`, `skill_view`, `activate_skill`, `load_skill`, and `use_skill` requests with a skill name. File reads recognize supported read tools and shell `cat`, `head`, `tail`, `sed`, `bat`, `less`, and `more` commands with literal `SKILL.md` paths. Codex code-mode requests can contain a literal `exec_command({cmd: ...})` call.

Each skill counts once per tool request. Two requests reading the same skill count twice. A single request reading that skill multiple times counts once. Repeated tool IDs are deduplicated within each harness, including copied archives and profile histories. ID-less Cursor export copies use session and tool positions; exports take precedence over matching native stores.

Names use the final category or plugin component: `development/tdd` becomes `tdd`, and `ponytail:ponytail` becomes `ponytail`. Different skills with the same short name therefore share a total. Removed skills can still appear in historical counts. Installed skills with no recorded requests are not listed.

## Coverage limits

- Only retained local structured histories are available. Remote, deleted, or unlogged activity is absent.
- Counts represent requests rather than confirmed successful executions. Activation followed by a separate read counts as two requests.
- Catalogs, prose, user/system messages, tool outputs, search commands, and heredoc examples are excluded.
- Bare `SKILL.md` filenames, variable paths, shell loops, Python reads, dynamic code, and unrecognized tools can be missed.
- Cursor copy deduplication assumes stable positions for exports without native IDs. Incomplete exports can omit native requests because exports take precedence.
- Text-only Cursor transcripts, unknown SQLite schemas, and non-JSON Cursor blobs are skipped and reported. Other plain text files are outside the scan.
- JSONL decoding is filtered to lines containing a candidate skill reference. Malformed unrelated lines are skipped without a malformed-record count.
- `not_found` means no configured root exists. `scanned` means discovered files were processed, not that every possible tool schema was recognized. `partial` means read errors, malformed candidates, unsupported databases, or text-only Cursor transcripts were found.
- A zero recognized count is not evidence that a harness never used skills.

SQLite connections use `mode=ro` and `query_only=ON`, including committed WAL data. The tool never evaluates logged code or invokes logged commands.
