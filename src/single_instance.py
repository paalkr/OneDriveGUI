"""
One OneDriveGUI per user and profiles file: a second start asks the running instance to show
its window and exits.
"""

import hashlib
import logging
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

ACTIVATE = b"activate\n"


def server_name(profiles_file):
    """Socket for this user and profiles file (so a GUI with another HOME, e.g. tests, is separate)."""
    digest = hashlib.sha256(os.path.abspath(profiles_file).encode()).hexdigest()[:12]
    name = f"OneDriveGUI-{os.getuid()}-{digest}"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        return os.path.join(runtime_dir, name)
    return name


def activate_running_instance(name, timeout_ms=1000):
    """True if another instance is listening; it has then been asked to show itself."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(ACTIVATE)
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """Listens for later starts; emits activate_requested when one asks to show the window."""

    activate_requested = Signal()

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._on_new_connection)
        # A socket left behind by a crashed instance would make listen() fail.
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            logging.warning(f"[GUI] Single-instance socket {name} unavailable: {self.server.errorString()}")

    def _on_new_connection(self):
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.readyRead.connect(lambda c=connection: self._on_ready_read(c))
            connection.disconnected.connect(connection.deleteLater)

    def _on_ready_read(self, connection):
        if ACTIVATE.strip() in bytes(connection.readAll()):
            logging.info("[GUI] Another start of OneDriveGUI asked to show the window")
            self.activate_requested.emit()
