"""
Automatic rebuild (--resync) of a Files On-Demand profile run by systemd.

The onedrive-ondemand package ships a one-shot user unit per profile that stops the profile's
background service, runs the client once with --on-demand-resync-once and starts the service
again when the rebuild succeeded. OneDriveGUI starts that unit, follows it (unit state plus the
client's D-Bus StateDetail; the one-shot client owns the same bus name as the service) and reports
the result. The normal service is never started after a failed rebuild.
"""

import logging
import subprocess

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ondemand_mode import journalctl, systemctl, ACTIVE_STATES

POLL_INTERVAL_MS = 1000
SHOW_PROPERTIES = "ActiveState,SubState,Result,InvocationID,LoadState"


def resync_unit_for_profile(profile_dir_name):
    """
    One-shot rebuild unit of ~/.config/<profile_dir_name>. Always the template instance, also for the
    default profile (installer c539994). Type=oneshot: activating while running, then inactive on
    success or failed; it stops the normal unit and starts it again only after a successful rebuild.
    """
    return f"onedrive-ondemand-resync@{profile_dir_name}.service"


def parse_show(output):
    values = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def unit_installed(unit, timeout=3):
    """True when systemd knows the unit file (LoadState=loaded). Blocking, but local and quick."""
    try:
        output = subprocess.run(
            [systemctl(), "--user", "show", unit, "--property=LoadState"], capture_output=True, text=True, timeout=timeout
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logging.info(f"[GUI] Could not ask systemd about {unit}: {e}")
        return False
    return parse_show(output).get("LoadState") == "loaded"


class ResyncRunner(QObject):
    """Runs and follows one rebuild unit. Emits progress(text) and finished(ok, message)."""

    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, unit, progress_source=None, parent=None):
        super().__init__(parent)
        self.unit = unit
        # Callable returning the current progress line from D-Bus (or "" when the client is not up).
        self.progress_source = progress_source or (lambda: "")
        self.invocation_before = None
        self.seen_new_invocation = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.poll)
        self.done = False

    def run(self, program, arguments, callback):
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.MergedChannels)

        def finished(exit_code, _status):
            output = bytes(process.readAll()).decode(errors="replace").strip()
            process.deleteLater()
            callback(exit_code, output)

        process.finished.connect(finished)
        process.start(program, arguments)

    def show(self, callback):
        self.run(systemctl(), ["--user", "show", self.unit, f"--property={SHOW_PROPERTIES}"], lambda _code, output: callback(parse_show(output)))

    def start(self):
        """Remember the unit's current invocation, then start it without waiting for the one-shot to end."""
        self.progress.emit("Starting the rebuild ...")

        def before(values):
            self.invocation_before = values.get("InvocationID", "")
            self.run(systemctl(), ["--user", "start", "--no-block", self.unit], started)

        def started(exit_code, output):
            logging.info(f"[GUI] systemctl --user start --no-block {self.unit}: {exit_code} {output}")
            if exit_code != 0:
                self.fail(f"systemctl --user start {self.unit} failed: {output}")
                return
            self.timer.start()

        self.show(before)

    def poll(self):
        def got(values):
            if self.done:
                return
            invocation = values.get("InvocationID", "")
            active = values.get("ActiveState", "unknown")
            if invocation and invocation != self.invocation_before:
                self.seen_new_invocation = True
            if not self.seen_new_invocation or active in ACTIVE_STATES:
                detail = self.progress_source()
                self.progress.emit(f"Rebuilding the local index ... {detail}".strip())
                return
            # The new invocation has ended.
            self.timer.stop()
            if active != "failed" and values.get("Result", "success") == "success":
                self.done = True
                self.finished.emit(True, "")
            else:
                self.fail(f"The rebuild ended with result {values.get('Result', 'unknown')}.")

        self.show(got)

    def fail(self, reason):
        self.timer.stop()
        if self.done:
            return
        self.done = True

        def journal(_code, lines):
            self.finished.emit(False, f"{reason}\n\n{lines}".strip())

        self.run(journalctl(), ["--user", "-u", self.unit, "-n", "20", "--no-pager"], journal)
