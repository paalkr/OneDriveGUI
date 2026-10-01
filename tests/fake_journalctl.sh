#!/bin/sh
# Stand-in for journalctl in tests (ONEDRIVEGUI_JOURNALCTL).
echo "$*" >> "$HOME/journalctl.log"
echo "okt. 02 10:00:00 host onedrive[123]: ERROR: fake failure for the test"
echo "okt. 02 10:00:01 host systemd[1]: onedrive-ondemand.service: Failed with result 'exit-code'."
