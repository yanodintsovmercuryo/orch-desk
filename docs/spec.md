# orchestrator-desk — design

A local-only (127.0.0.1) page for the owner of team-skills-orchestrator streams: epics and the tasks in work, nothing else.
Appearance follows the orchestrator's own viewer (tso-view: system faces, hairline borders, 44 px rows, 4 px radius, its colour tokens),
without its filters, search or navigation bar.

## What the page shows

1. **Epics** (Linear): parents of the streams' tracker cards with done / in work / total over leaf cards, sub-epics folded under them;
   finished and empty ones are hidden; statistics appear 0.5 s after the pointer rests on a row.
2. **Tasks in work**: open streams only, each with the seven-step road (grey done, light current and pulsing), the stage, PR, age and,
   when the owner has a question for it, a question icon. A row opens the task page.
3. **Task page**: links, stage milestones, the owner's questions with options and a recommendation, a terminal prompt of the stream's own
   session, a reply to the orchestrator, the journal.

## Sources

| Data | Source |
|---|---|
| Orchestrators, streams, journal | the plugin's files under `$ORCHESTRATORS_HOME/<role>/<name>/` (`registry.yaml`, `streams/<id>/card.md`, `journal.md`); read only |
| Checkpoint order | plugin `scripts/lib/checkpoints.tsv` (latest cached version), built-in fallback |
| Owner questions | `tso ask list --all` and `tso ask show q-N` (tsod); answered with `tso answer q-N --option K [--text …]` |
| Messages to the orchestrator | `tso send <role>/<name> <text>`; the Orca terminal only when nobody holds the name under tso (exit 5) |
| Links, epics | `linear issues …`, `gh pr list --head <branch>`; stale-while-revalidate caches in `~/.local/state/desk/` |
| Stream terminal prompts | `claude agents --json` + `orca terminal read` of the stream's worktree session |

## Questions

An orchestrator files a question with `tso ask new` (the `desk ask` command is a thin front over it). tsod binds the asker from the calling
process's ancestry; the desk only reads and answers. `tso answer` refuses processes with a Claude session among their ancestors, which the
launchd-started desk server is not. One call answers one question. A question belongs to the stream named by `--stream`, or by a task id in
its title; the rest are shown above the epics.

## Stage of a task

The latest ordered checkpoint in the journal (the guard writes the rows). With no journal at all the stage is read off the PR (merged →
merge, open → publication, none → plan) and marked with `*`. A stream with no journal is shown only while a session runs in its worktree.

## Safety

The server binds 127.0.0.1, serves one HTML file, checks `Host` and `Origin` on every POST, accepts JSON only, and loads no external
resources. The plugin's files are never written by the desk.

## Layout

`server.py` (stdlib HTTP, a single-flight snapshot of `/api/state`), `desk/state.py`, `desk/view.py`, `desk/tsoq.py`, `desk/deliver.py`,
`desk/epics.py`, `desk/links.py`, `desk/terminal.py`, `desk/transcript.py` (only for the Stop hook), `desk/uploads.py`, `hooks/stop_asks.py`,
`bin/desk`, `web/index.html`, `watchdog.sh`, `janitor/` (manual tools), `tests/`.
