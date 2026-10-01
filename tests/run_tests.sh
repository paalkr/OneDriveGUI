#!/bin/sh
# Runs the D-Bus integration tests isolated from the desktop session:
#  - a private session bus (dbus-run-session), so neither the mock nor the GUI sees a real client,
#  - a throw-away HOME with two OneDriveGUI profiles, so no real profile or config is read or written,
#  - QT_QPA_PLATFORM=offscreen, so no window opens.
# Usage: tests/run_tests.sh [python]   (default: .venv/bin/python)
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
python=${1:-$repo/.venv/bin/python}

home=$(mktemp -d)
trap 'rm -rf "$home"' EXIT

mkdir -p "$home/.config/onedrive-gui" "$home/.config/profile-a" "$home/.config/profile-b"
printf 'sync_dir = "%s/OneDrive-a"\n' "$home" > "$home/.config/profile-a/config"
printf 'sync_dir = "%s/OneDrive-b"\n' "$home" > "$home/.config/profile-b/config"
cat > "$home/.config/onedrive-gui/profiles" <<EOF
[profile-a]
config_file = $home/.config/profile-a/config
auto_sync = True
account_type =
free_space =
ondemand = True
systemd_unit = onedrive-ondemand@profile-a.service

[profile-b]
config_file = $home/.config/profile-b/config
auto_sync = False
account_type =
free_space =
EOF

cd "$repo"
env HOME="$home" ONEDRIVEGUI_TEST_HOME=1 QT_QPA_PLATFORM=offscreen \
    dbus-run-session -- "$python" tests/test_ondemand.py
