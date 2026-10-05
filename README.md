# agent-skill-counts

Count recorded skill activations and `SKILL.md` reads across your local coding-agent histories. Get totals per skill, a breakdown by harness, and a coverage report.

Python 3.10+. No runtime dependencies. No network requests. Harness histories are opened for reading; reports are written only when you pass `--out`.

## Install and run

Install the released CLI with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install 'git+https://github.com/xlyk/agent-skill-counts@v0.1.0'
agent-skill-counts --out ./skill-usage-report
```

Or run from a checkout without installing:

```sh
git clone https://github.com/xlyk/agent-skill-counts.git
cd agent-skill-counts
python3 agent_skill_counts.py --out ./skill-usage-report
```

Open `skill-usage-report/totals.csv` for the ranked skill counts. Use `counts.csv` for the per-harness breakdown and `counts.json` for coverage and counting limitations.

To scan a copied home directory or an additional history source:

```sh
agent-skill-counts --home /path/to/copied-home --out ./skill-usage-report
agent-skill-counts --extra claude=/path/to/project-history --out ./skill-usage-report
```

Additional sources must use a recognized structured tool-call format. `--extra` adds paths; it does not add a parser for an unknown log format.

## Counting and coverage

See the [reference](REFERENCE.md) for source paths, flags, output fields, and limitations.

The totals count **tool requests**, combining explicit activations with skill-file reads. They do not prove that a request succeeded or that the agent followed the instructions. Skill catalogs, conversation mentions, search results, and tool outputs are excluded.

Native histories were checked for Codex, Claude Code, Pi, Oh My Pi, Grok, Hermes, Cursor, and OpenCode on macOS. Other discovery paths are candidates, not verified integrations. Deleted, remote, and unlogged activity cannot be recovered. Dynamic shell reads can be missed.

Reports contain skill names, counts, and local source paths. They do not contain conversation text or tool arguments. Keep reports local unless you intend to share those details.

## Check the tool

```sh
agent-skill-counts --self-test
```

The self-test uses temporary synthetic JSONL histories and SQLite databases. It checks native request formats, false-positive exclusions, duplicate histories, multiple requests in one message, and CSV/JSON output through the CLI.

MIT licensed. Contributions for additional native formats should include an anonymized synthetic record in the self-test.
