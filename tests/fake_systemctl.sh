#!/bin/sh
# Stand-in for systemctl in tests (ONEDRIVEGUI_SYSTEMCTL). Unit states live in
# $HOME/systemd/<unit> as "<ActiveState> <UnitFileState>"; extra `show` properties
# (Result=, InvocationID=) in $HOME/systemd/<unit>.props; LoadState=loaded when
# $HOME/systemd/<unit>.loaded exists. Every call is logged to $HOME/systemctl.log.
# The real systemd user manager is never asked.
echo "$*" >> "$HOME/systemctl.log"
[ "$1" = --user ] && shift
cmd=$1
shift
[ "$1" = --no-block ] && shift
unit=$1
dir="$HOME/systemd"
mkdir -p "$dir"
state="inactive disabled"
[ -f "$dir/$unit" ] && state=$(cat "$dir/$unit")
case $cmd in
  show)
    echo "ActiveState=${state% *}"
    echo "UnitFileState=${state#* }"
    if [ -f "$dir/$unit.loaded" ]; then echo "LoadState=loaded"; else echo "LoadState=not-found"; fi
    [ -f "$dir/$unit.props" ] && cat "$dir/$unit.props"
    ;;
  start|stop|restart)
    if [ -f "$dir/$unit.fail" ]; then echo "Job for $unit failed."; exit 1; fi
    case $unit in
      *resync*)
        # A one-shot rebuild: running until the test writes its final state.
        echo "activating ${state#* }" > "$dir/$unit"
        printf 'Result=success\nInvocationID=%s\n' "$(date +%s%N)" > "$dir/$unit.props" ;;
      *)
        [ "$cmd" = stop ] && new=inactive || new=active
        echo "$new ${state#* }" > "$dir/$unit" ;;
    esac ;;
  enable) echo "${state% *} enabled" > "$dir/$unit"; echo "Created symlink for $unit" ;;
  disable) echo "${state% *} disabled" > "$dir/$unit"; echo "Removed symlink for $unit" ;;
  *) exit 1 ;;
esac
