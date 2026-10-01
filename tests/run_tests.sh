#!/bin/sh
# Runs the D-Bus integration tests isolated from the desktop session:
#  - a private session bus (dbus-run-session), so neither the mock nor the GUI sees a real client,
#  - a throw-away HOME with two OneDriveGUI profiles, so no real profile or config is read or written,
#  - QT_QPA_PLATFORM=offscreen, so no window opens.
# Usage: [PYTHON=...] tests/run_tests.sh [unittest arguments, e.g. a test name]   (default: .venv/bin/python)
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
python=${PYTHON:-$repo/.venv/bin/python}

home=$(mktemp -d)
trap 'rm -rf "$home"' EXIT

mkdir -p "$home/.config/onedrive-gui" "$home/.config/profile-a" "$home/.config/profile-b"
printf 'sync_dir = "%s/OneDrive-a"\n' "$home" > "$home/.config/profile-a/config"
printf 'sync_dir = "%s/OneDrive-b"\n' "$home" > "$home/.config/profile-b/config"
# Keep the GUI's own log inside the temporary HOME.
printf '[SETTINGS]\nlog_file = %s/onedrive-gui.log\ndebug_level = INFO\n' "$home" > "$home/.config/onedrive-gui/gui_settings"
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
env HOME="$home" ONEDRIVEGUI_TEST_HOME=1 QT_QPA_PLATFORM=offscreen ONEDRIVEGUI_SYSTEMCTL="$repo/tests/fake_systemctl.sh" \
    dbus-run-session -- "$python" tests/test_ondemand.py "$@"
