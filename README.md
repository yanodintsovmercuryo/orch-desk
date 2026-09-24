# orchestrator-desk

Local page over team-skills-orchestrator streams: who waits for the owner, what is asked, a reply
form into the orchestrator's Orca terminal, progress and milestone log per stream.

- Open: http://127.0.0.1:8800
- Install / reload the launchd agent: `./install.sh`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.yanodintsov.orchestrator-desk`
- Stop: `launchctl bootout gui/$(id -u)/com.yanodintsov.orchestrator-desk`
- Log: `desk.log`; sent replies: `replies.jsonl`
- Tests: `python3 -m unittest discover -s tests -t .`

The plugin is read, never modified. Design and data sources: `docs/spec.md`.
