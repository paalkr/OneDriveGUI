"""
How a profile runs (Files On-Demand or not; background service or started by OneDriveGUI),
the state of its systemd user unit, and "free up space" for a whole on-demand mount.
"""

import logging
import os
import time

from PySide6.QtCore import QObject, QProcess, Signal

from ondemand_dbus import is_ondemand_profile, profile_systemd_unit

# Overridable so tests never talk to the user's systemd manager.
SYSTEMCTL_ENV = "ONEDRIVEGUI_SYSTEMCTL"
UNIT_STATE_MAX_AGE = 5  # seconds

RUN_SERVICE = "Runs as background service (systemd)"
RUN_GUI = "Started by OneDriveGUI"
RUN_OUTSIDE = "Started outside OneDriveGUI"
ONDEMAND_BADGE = "Files On-Demand"


def systemctl():
    return os.environ.get(SYSTEMCTL_ENV, "systemctl")


def parse_unit_show(output):
    """`systemctl show --property=ActiveState,UnitFileState` output -> (active, enabled)."""
    values = {}
    for line in output.splitlines():
        key, _sep, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values.get("ActiveState") or "unknown", values.get("UnitFileState") or "not installed"


class UnitStates(QObject):
    """Cached ActiveState / UnitFileState of systemd user units, refreshed asynchronously."""

    changed = Signal(str)  # unit

    def __init__(self, parent=None):
        super().__init__(parent)
        self.states = {}  # unit -> (active, enabled, timestamp)
        self.running = {}  # unit -> QProcess

    def state(self, unit):
        """Last known (active, enabled); starts a refresh when the value is old."""
        cached = self.states.get(unit)
        if cached is None or time.time() - cached[2] > UNIT_STATE_MAX_AGE:
            self.refresh(unit)
        return (cached[0], cached[1]) if cached else ("unknown", "unknown")

    def refresh(self, unit):
        if not unit or unit in self.running:
            return
        process = QProcess(self)
        self.running[unit] = process

        def finished(_exit_code, _status):
            output = bytes(process.readAllStandardOutput()).decode(errors="replace")
            active, enabled = parse_unit_show(output)
            previous = self.states.get(unit)
            self.states[unit] = (active, enabled, time.time())
            self.running.pop(unit, None)
            process.deleteLater()
            if previous is None or previous[:2] != (active, enabled):
                self.changed.emit(unit)

        process.finished.connect(finished)
        process.start(systemctl(), ["--user", "show", unit, "--property=ActiveState,UnitFileState"])

    def set_enabled(self, unit, enabled, callback=None):
        """`systemctl --user enable|disable <unit>` (start at login); callback(ok, output)."""
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.MergedChannels)

        def finished(exit_code, _status):
            output = bytes(process.readAll()).decode(errors="replace").strip()
            logging.info(f"[GUI] systemctl --user {'enable' if enabled else 'disable'} {unit}: {exit_code} {output}")
            self.states.pop(unit, None)
            self.refresh(unit)
            process.deleteLater()
            if callback:
                callback(exit_code == 0, output)

        process.finished.connect(finished)
        process.start(systemctl(), ["--user", "enable" if enabled else "disable", unit])


def profile_mode(profile_config, instance=None, gui_owns_worker=False, unit_states=None):
    """
    Facts shown wherever a profile appears.
    Returns dict: ondemand (bool), run (one of RUN_*), unit ('' if none), active, enabled.
    """
    ondemand = bool(instance.props.get("OnDemand")) if instance is not None else is_ondemand_profile(profile_config)
    unit = profile_systemd_unit(profile_config) if is_ondemand_profile(profile_config) else ""
    active, enabled = unit_states.state(unit) if unit and unit_states else ("unknown", "unknown")

    if gui_owns_worker:
        run = RUN_GUI
    elif unit and (active in ("active", "activating", "reloading") or (instance is None and is_ondemand_profile(profile_config))):
        run = RUN_SERVICE
    elif instance is not None:
        run = RUN_OUTSIDE
    else:
        run = RUN_GUI
    return {"ondemand": ondemand, "run": run, "unit": unit, "active": active, "enabled": enabled}


def mode_text(mode, separator=" - ", include_unit=True):
    parts = []
    if mode["ondemand"]:
        parts.append(ONDEMAND_BADGE)
    parts.append(mode["run"])
    if mode["unit"] and include_unit:
        parts.append(f"{mode['unit']}: {mode['active']}")
    return separator.join(parts)


def free_up_space(mount):
    """
    Ask the on-demand mount to dehydrate everything (user.onedrive.action=free on each top-level
    item; directories are queued by the client and unpin what is below them first).
    Returns (accepted names, {name: error}). Blocking: run it off the GUI thread.
    """
    accepted, refused = [], {}
    with os.scandir(mount) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            try:
                os.setxattr(entry.path, "user.onedrive.action", b"free", follow_symlinks=False)
                accepted.append(entry.name)
            except OSError as e:
                refused[entry.name] = e.strerror or str(e)
    return accepted, refused
