#!/bin/sh
# Stand-in for journalctl in tests (ONEDRIVEGUI_JOURNALCTL). Prints $HOME/journal/<unit> when a
# test wrote one (the unit is the argument after -u), otherwise a fixed failure.
echo "$*" >> "$HOME/journalctl.log"
unit=""
while [ $# -gt 0 ]; do [ "$1" = -u ] && unit=$2; shift; done
if [ -n "$unit" ] && [ -f "$HOME/journal/$unit" ]; then cat "$HOME/journal/$unit"; exit 0; fi
echo "okt. 02 10:00:00 host onedrive[123]: ERROR: fake failure for the test"
echo "okt. 02 10:00:01 host systemd[1]: onedrive-ondemand.service: Failed with result 'exit-code'."
