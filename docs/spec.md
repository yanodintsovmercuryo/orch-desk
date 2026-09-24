# orchestrator-desk — design

A local-only (127.0.0.1) page for the owner of team-skills-orchestrator streams.

## Goals

1. Status of every stream: phase, whose turn, progress over the checkpoint order.
2. Which streams wait for the owner.
3. The request the owner is asked to answer.
4. A reply form delivered straight to the orchestrator's terminal.
5. A per-stream milestone log with links to the PR and the Linear task.

## Hard constraints

- The plugin is never modified. Its state is read only; a plugin update that changes a format
  surfaces as a parse error on the page, never as a crash or a write.
- Nothing leaves the machine beyond what agents already send: the server binds 127.0.0.1, the page
  loads no external resources.
- Look: the Hub console (`@mercuryopro/tokens`) — system font, panels, one task table — in a
  strictly neutral black-and-white dark theme. Only streams the orchestrator names as waiting for
  the owner are marked and get a reply form.

## Sources (read only)

| Data | Source |
|---|---|
| Orchestrators | `$ORCHESTRATORS_HOME/<role>/<name>/registry.yaml` (`session_id`, `transcript`) |
| Streams | `…/streams/<id>/card.md`: header (`repository`, `branch`, `tracker`) and the derived block (`phase`, `owed by`, `owed`) |
| Log | `…/streams/<id>/journal.md`: TSV `ts stream checkpoint in|out actor digest`; other lines are notes `ts stream text` |
| Checkpoint order | plugin `scripts/lib/checkpoints.tsv` (latest cached version), built-in fallback |
| Owner asks | orchestrator transcript JSONL: the last assistant text carrying a turn-end status line `… · waiting: ты — item; item · …` |
| Links | `linear issues read` (title, state), `gh pr list --head <branch>`; cached 5 min |

## Waiting for the owner

The turn-end line's `waiting:` segment addressed to the owner (`ты`, `you`, `owner`) is split into
items by `;`. An item belongs to a stream when it names the stream id or the stream's PR number.
Items naming no stream are shown as general asks. The request text for a stream is the set of
paragraphs of that last orchestrator message that mention the stream id or its PR number; the full
message is available on demand.

## Progress

Ordered checkpoints: launched, spec-drafted, plan-passed, gate-round, pre-publish, pre-merge,
closed. Progress is the position of the latest ordered checkpoint in the journal; `intent-moved`
keeps the position and flags the stream.

## Reply delivery

`registry.session_id` → `claude agents --json` (pid) → `ORCA_TERMINAL_HANDLE` in the process
environment → `orca terminal list` (connected, writable) → `orca terminal send --text … --enter`.
The text is `[desk] <stream>: <reply>` on one line (newlines become ` / `). Every attempt is
appended to `replies.jsonl` with the orca receipt.

`POST /api/reply` accepts only `application/json` with a `Host` of the server and an `Origin` of the
server, so another site open in the browser cannot type into the orchestrator.

## Layout

`server.py` (stdlib HTTP), `desk/state.py`, `desk/transcript.py`, `desk/links.py`,
`desk/deliver.py`, `web/index.html` (polls `/api/state` every 5 s), `launchd/` agent with
`KeepAlive`, `tests/` on fixtures.
