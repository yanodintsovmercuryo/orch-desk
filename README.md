# orchestrator-desk

Local page over team-skills-orchestrator streams: who waits for the owner, what is asked, a reply
form into the orchestrator's Orca terminal, progress and milestone log per stream.

- Open: http://127.0.0.1:8800
- Install / reload the launchd agent: `./install.sh`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.yanodintsov.orchestrator-desk`
- Stop: `launchctl bootout gui/$(id -u)/com.yanodintsov.orchestrator-desk`
- Log: `desk.log`; sent replies: `replies.jsonl`
- Tests: `python3 -m unittest discover -s tests -t .`

Owner asks: `desk ask|asks|answers|done|withdraw|show` (symlinked to `~/.local/bin/desk`); records live in
`~/.local/state/desk/asks/`. A Stop hook (`hooks/stop_asks.py`, registered in `~/.claude/settings.json`)
keeps the orchestrator from ending a turn with an owner ask outside desk or an answer unread. The
supervisor (`desk/supervisor.py`) runs inside the server; its events and toggles are on the page.

The plugin is read, never modified. Design and data sources: `docs/spec.md`.
