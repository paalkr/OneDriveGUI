#!/bin/sh
# Stand-in for systemctl in tests (ONEDRIVEGUI_SYSTEMCTL). Unit states live in
# $HOME/systemd/<unit> as "<ActiveState> <UnitFileState>"; every call is logged to
# $HOME/systemctl.log. The real systemd user manager is never asked.
echo "$*" >> "$HOME/systemctl.log"
[ "$1" = --user ] && shift
cmd=$1
unit=$2
mkdir -p "$HOME/systemd"
state="inactive disabled"
[ -f "$HOME/systemd/$unit" ] && state=$(cat "$HOME/systemd/$unit")
case $cmd in
  show) echo "ActiveState=${state% *}"; echo "UnitFileState=${state#* }" ;;
  start|stop)
    if [ -f "$HOME/systemd/$unit.fail" ]; then echo "Job for $unit failed."; exit 1; fi
    [ "$cmd" = start ] && new=active || new=inactive
    echo "$new ${state#* }" > "$HOME/systemd/$unit" ;;
  enable) echo "${state% *} enabled" > "$HOME/systemd/$unit"; echo "Created symlink for $unit" ;;
  disable) echo "${state% *} disabled" > "$HOME/systemd/$unit"; echo "Removed symlink for $unit" ;;
  *) exit 1 ;;
esac
