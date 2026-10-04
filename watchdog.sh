#!/bin/sh
# Restarts the desk when /api/state does not answer within 20 s (it hangs now and then).
curl -sf -m 20 -o /dev/null http://127.0.0.1:8800/api/state && exit 0
echo "$(date '+%F %T') desk did not answer, restarting" >> /Users/yanodintsov/tools/orchestrator-desk/watchdog.log
launchctl kickstart -k "gui/$(id -u)/com.yanodintsov.orchestrator-desk"
