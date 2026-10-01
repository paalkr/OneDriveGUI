#!/usr/bin/env python3

import os
import sys
import signal
import logging
import copy

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


from logger import logger


from global_config import DIR_PATH, PROFILES_FILE
from single_instance import InstanceServer, activate_running_instance, server_name


app = QApplication(sys.argv)
app.setApplicationName("OneDriveGUI")
app.setDesktopFileName("OneDriveGUI")
app.setWindowIcon(QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png"))

# Started again (launcher entry, terminal): show the running instance instead of running twice.
instance_name = server_name(PROFILES_FILE)
if __name__ == "__main__" and activate_running_instance(instance_name):
    logging.info("[GUI] OneDriveGUI is already running; asked it to show its window")
    sys.exit(0)


from options import gui_settings, global_config, version
from global_config import save_global_config
from main_window import MainWindow


def main_window_start_state():
    # Determine if OneDriveGUI should start maximized, minimized to tray or minimized to taskbar/dock.
    # This should help ensure the GUI does not just disappear on Gnome without system tray extension.

    if gui_settings.get("start_minimized") == "True" or len(global_config) == 0:
        try:
            if main_window.tray.isSystemTrayAvailable():
                main_window.hide()
                logging.info("[GUI] Starting OneDriveGUI minimized to system tray")
        except:
            main_window.show()
            main_window.setWindowState(Qt.WindowMinimized)
            logging.info("[GUI] Starting OneDriveGUI minimized to taskbar/dock")
    else:
        main_window.show()
        logging.info("[GUI] Starting OneDriveGUI maximized")


workers = {}


if __name__ == "__main__":
    logging.info(f"Starting OneDriveGUI v{version}")

    main_window = MainWindow()

    # After MainWindow: it discovers clients on D-Bus, and the configs of profiles attached to
    # them (or Files On-Demand profiles) must not be rewritten here.
    if len(global_config) > 0:
        save_global_config(global_config)
    main_window_start_state()

    instance_server = InstanceServer(instance_name)
    instance_server.activate_requested.connect(main_window.show_and_raise)

    # With a tray the GUI lives on after its last window closes (the main window only hides);
    # without one, the default stays so closing still ends the application.
    main_window.configure_quit_on_last_window_closed(app)

    # SIGTERM/SIGINT: stop the clients the GUI started, then quit. The timer lets the Python
    # signal handlers run while Qt's event loop is waiting.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_args: QTimer.singleShot(0, main_window.shutdown))
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(500)

    sys.exit(app.exec())
