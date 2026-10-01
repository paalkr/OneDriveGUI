from PySide6.QtCore import QTimer, QUrl, QFileInfo, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QWidget,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QListWidgetItem,
    QStackedLayout,
    QMessageBox,
    QStyledItemDelegate,
    QStyle,
    QApplication,
)


# Imports for main window.
from ui.ui_mainwindow import Ui_MainWindow
from ui.ui_process_status_page import Ui_status_page


# Import for login windows.
from ui.ui_external_login import Ui_ExternalLoginWindow


import re
import os
import sys

import requests
import subprocess


from wizard import setup_wizard
from profile_settings_window import profile_settings_window, set_profile_item_tooltip

from options import (
    global_config,
    client_bin_path,
    gui_settings,
    version,
)

from utils.utils import development_build_label, humanize_file_size, shorten_path, format_relative_time
from workers import WorkerThread, MaintenanceWorker, TaskList, workers
from ondemand_dbus import (
    OneDriveDBus,
    aggregate_tray_state,
    match_profiles,
    profile_confdir,
    profile_systemd_unit,
    read_weburl,
    start_decision,
    tray_state,
)
from ondemand_mode import ACTIVE_STATES, UnitStates, mode_text, profile_mode
from ondemand_ui import STATE_LABELS, OnDemandStatusWindow, file_type_icon, status_text, themed_icon, tray_icon
from datetime import datetime
from gui_settings_window import gui_settings_window

import logging

# from logger import logger
from global_config import DIR_PATH, PROFILES_FILE, save_global_config, set_attached_check

try:
    from ui.ui_login import Ui_LoginWindow
except ImportError:
    logging.warning("Failed to import ui_login. This is expected if you are running AppImage version.")

    # Define a dummy class to avoid errors when this UI component isn't available
    class Ui_LoginWindow:
        def setupUi(self, window):
            logging.error("LoginWindow UI is not available in this environment")
            window.hide()


class MainWindow(QMainWindow, Ui_MainWindow):
    # Emitted when how a profile runs (on-demand, service/GUI, unit state) or its client state changed.
    mode_indicators_changed = Signal()
    # profile, "start"/"stop", ok, message, new ActiveState
    service_action_finished = Signal(str, str, bool, str, str)
    # profile, progress line / (profile, ok, message) of an automatic rebuild (--resync)
    resync_progress = Signal(str, str)
    resync_finished = Signal(str, bool, str)

    def __init__(self):
        # Expose this instance as main_window.main_window_instance so other modules
        # (e.g. profile_settings_window) can reach it without a circular import.
        import main_window

        main_window.main_window_instance = self

        # Access setup_wizard from wizard module to avoid circular imports
        import wizard

        wizard.setup_wizard.show_main_window_signal.connect(self.show)
        wizard.setup_wizard.add_profile_signal.connect(self.add_profile)

        profile_settings_window.remove_profile_signal.connect(self.remove_profile)
        profile_settings_window.rename_profile_signal.connect(self.rename_profile_in_main_window)

        super(MainWindow, self).__init__()
        self.setupUi(self)
        self.setWindowTitle(f"OneDriveGUI v{version}")
        self.setWindowIcon(QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png"))
        self.trash_icon = QApplication.style().standardIcon(QStyle.SP_TrashIcon)

        if gui_settings.get("frameless_window") == "True":
            self.setWindowFlags(Qt.FramelessWindowHint)

        if len(global_config) == 0:
            self.show_setup_wizard()

        self.pushButton_new_profile.hide()
        self.menubar.hide()

        self.comboBox.activated.connect(self.switch_account_status_page)
        self.comboBox.setStyleSheet(
            f"""
        QComboBox {{
            border : 0px black;
            border-radius : 0px;
            background-color: rgb(0, 120, 212);
            color: rgb(255, 255, 255);
        }}

        QComboBox::drop-down {{
            border : 0px black;
            background-color: rgb(0, 120, 212);
            text-transform: uppercase;
            color: rgb(255, 255, 255);
        }}

        QComboBox QAbstractItemView::item {{ 
            min-height: 20px;
            background-color: rgb(0, 120, 212);
            text-transform: uppercase;
            color: rgb(255, 255, 255);
        }}

        QComboBox::down-arrow {{
            image: url({DIR_PATH}/resources/images/user-account.png);
            width: 20px;
            height: 20px;
        }}

        background-color: rgb(0, 120, 212);
        color: rgb(255, 255, 255);
        
        """
        )
        self.stackedLayout = QStackedLayout()

        # Account types already persisted in the profiles file. Used to detect
        # when a type reported during sync still needs to be saved to disk.
        self.saved_account_types = {profile: global_config[profile]["account_type"] for profile in global_config}

        self.profile_status_pages = {}
        for profile in global_config:
            self.comboBox.addItem(profile)
            self.profile_status_pages[profile] = ProfileStatusPage(profile)
            self.profile_status_pages[profile].start_sync_signal.connect(self.start_onedrive_monitor)
            self.profile_status_pages[profile].stop_sync_signal.connect(self.stop_onedrive_monitor)
            self.profile_status_pages[profile].quit_gui_signal.connect(self.graceful_shutdown)
            self.stackedLayout.addWidget(self.profile_status_pages[profile])

            # Connect scroll events for timestamp updates
            listWidget = self.profile_status_pages[profile].listWidget
            scrollbar = listWidget.verticalScrollBar()
            scrollbar.valueChanged.connect(self.on_list_scrolled)

        self.verticalLayout_2.addLayout(self.stackedLayout)

        # Ensure all profiles in comboBox are aligned to center
        delegate = AlignDelegate(self.comboBox)
        self.comboBox.setItemDelegate(delegate)

        # Making comboBox editable, which allows for center alignment.
        self.comboBox.setEditable(True)
        line_edit = self.comboBox.lineEdit()
        line_edit.setAlignment(Qt.AlignRight)
        line_edit.setReadOnly(True)

        # Hide comboBox if only one profile exists
        if len(self.profile_status_pages) < 2:
            self.comboBox.hide()

        # Clients that expose the D-Bus status interface (e.g. a Files On-Demand client run by
        # a systemd user unit). Profiles with such a client are "attached": the GUI reads their
        # status over D-Bus and never spawns or stops a client for them. Without jeepney or a
        # session bus, self.dbus is None and everything below behaves as upstream.
        self.attached = {}  # profile name -> bus name
        self.unit_states = UnitStates(self)
        self.unit_states.changed.connect(lambda _unit: self.refresh_mode_indicators())
        self.mode_texts = {}
        self.service_actions = set()  # profiles with a start/stop/restart/rebuild in flight
        self.resync_runners = {}
        self.status_windows = {}
        self.attached_transfer_items = {}  # profile name -> {path: (list widget, direction, total)}
        self.tray_menu_signature = None
        self.dbus = OneDriveDBus(self)
        set_attached_check(self.profile_has_dbus_client)
        self.matched_config_files = None
        if self.dbus.start():
            for signal in (self.dbus.instance_added, self.dbus.instance_removed):
                signal.connect(self.match_dbus_instances)
            for signal in (self.dbus.properties_changed, self.dbus.issues_changed):
                signal.connect(self.on_dbus_instance_changed)
            self.dbus.transfers_changed.connect(self.on_dbus_transfers_changed)
        else:
            self.dbus = None

        # System Tray
        self.tray = QSystemTrayIcon()
        if self.tray.isSystemTrayAvailable():
            # Initial icon will be updated by update_tray_icon() after profiles are loaded
            icon = QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png")
            self.tray_menu = QMenu()
            self.refresh_tray_menu()

            self.tray.activated.connect(self.tray_icon_clicked)

            self.tray.setIcon(icon)
            self.tray.setContextMenu(self.tray_menu)
            self.tray.show()
            # Initial tooltip - will be updated by update_tray_icon()
            self.tray.setToolTip("OneDriveGUI")

        else:
            self.tray = None

        self.match_dbus_instances()

        # Version check results storage
        self.client_version_status = {
            "label_text": "",
            "tooltip_text": "",
            "min_requirements_met": True,
            "info_text": "",  # e.g. "Development build ...": shown without a warning
        }
        self.gui_version_status = {
            "label_text": "",
            "tooltip_text": "",
        }

        self.refresh_process_status = QTimer()
        self.refresh_process_status.setSingleShot(False)
        self.refresh_process_status.timeout.connect(self.onedrive_process_status)
        self.refresh_process_status.start(500)

        self.check_client_version = QTimer()
        self.check_client_version.setSingleShot(True)
        self.check_client_version.timeout.connect(self.client_version_check)
        self.check_client_version.start(500)

        self.check_gui_version = QTimer()
        self.check_gui_version.setSingleShot(True)
        self.check_gui_version.timeout.connect(self.gui_version_check)
        self.check_gui_version.start(1000)

        self.auto_sync = QTimer()
        self.auto_sync.setSingleShot(True)
        self.auto_sync.timeout.connect(self.autostart_monitor)
        self.auto_sync.start(1000)

        # Timer to refresh relative times for completed transfers
        self.refresh_relative_times = QTimer()
        self.refresh_relative_times.setSingleShot(False)
        self.refresh_relative_times.timeout.connect(self.update_relative_times)
        self.refresh_relative_times.start(60000)  # Update every 60 seconds

        # Debounce timer for scroll-triggered updates
        self.scroll_update_timer = QTimer()
        self.scroll_update_timer.setSingleShot(True)
        self.scroll_update_timer.timeout.connect(self.update_relative_times)
        self.scroll_update_timer.setInterval(200)  # 200ms debounce delay

    def mousePressEvent(self, event):
        if gui_settings.get("frameless_window") == "True":
            self.dragPos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if gui_settings.get("frameless_window") == "True":
            self.move(self.pos() + event.globalPosition().toPoint() - self.dragPos)
            self.dragPos = event.globalPosition().toPoint()
            event.accept()

    def tray_icon_clicked(self, reason):
        if reason == QSystemTrayIcon.Unknown:
            pass
        elif reason == QSystemTrayIcon.Context:
            logging.debug("[GUI] Right clicked on tray icon")
        elif reason == QSystemTrayIcon.DoubleClick:
            pass
        elif reason == QSystemTrayIcon.Trigger:
            logging.debug("[GUI] Left clicked on tray icon")
            if not self.isVisible():
                self.show()
            elif self.isActiveWindow():
                self.hide()
            else:
                self.activateWindow()
                self.raise_()
        elif reason == QSystemTrayIcon.MiddleClick:
            pass
        else:
            pass

    def graceful_shutdown(self):
        question = f"Would you like to stop all sync operations and quit OneDriveGUI ?"
        external = [profile for profile in self.attached if profile not in workers]
        if external:
            question = (
                "Would you like to stop the sync operations started by OneDriveGUI and quit OneDriveGUI ?<br><br>"
                f"Clients started outside OneDriveGUI keep running: {', '.join(external)}"
            )

        close_question = QMessageBox.question(
            self,
            "Quit OneDriveGUI ?",
            question,
            buttons=QMessageBox.Yes | QMessageBox.No,
            defaultButton=QMessageBox.No,
        )

        if close_question == QMessageBox.Yes:
            self.shutdown()

        elif close_question == QMessageBox.No:
            logging.info("[GUI] Keeping OneDriveGUI running.")

    def shutdown(self):
        """Stop the clients the GUI started and quit. Attached clients are never stopped."""
        logging.info("Quitting OneDriveGUI")
        workers_to_stop = []

        for worker in workers:
            workers_to_stop.append(worker)

        for worker in workers_to_stop:
            workers[worker].stop_worker()

        if self.dbus:
            self.dbus.stop()

        if self.tray:
            self.tray.hide()
        # exit(), not quit(): Qt 6's quit() first closes every window and gives up when one refuses,
        # which the main window does (it hides to the tray instead).
        QApplication.exit(0)

    def configure_quit_on_last_window_closed(self, app):
        app.setQuitOnLastWindowClosed(not (self.tray and self.tray.isSystemTrayAvailable()))

    def show_and_raise(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.activateWindow()
        self.raise_()

    def stop_onedrive_monitor(self, profile_name):
        if profile_name in workers:
            workers[profile_name].stop_worker()
            self.profile_status_pages[profile_name].label_onedrive_status.setText("OneDrive sync has been stopped")
            logging.info(f"OneDrive sync for profile {profile_name} has been stopped.")
        else:
            logging.info(f"OneDrive for profile {profile_name} is not running.")

    def closeEvent(self, event):
        # Minimize main window to system tray if it is available. Otherwise minimize to taskbar.
        event.ignore()
        try:
            if self.tray.isSystemTrayAvailable():
                self.hide()
                logging.debug("[GUI] Minimizing main window to tray")
            else:
                self.setWindowState(Qt.WindowMinimized)
                logging.debug("[GUI] Minimizing main window to taskbar/dock")
        except:
            self.setWindowState(Qt.WindowMinimized)
            logging.debug("[GUI] Minimizing main window to taskbar/dock")

    def show_setup_wizard(self):
        setup_wizard.show()

    def show_settings_window(self):
        profile_settings_window.show()

        # Start checking for unsaved changes.
        profile_settings_window.start_unsaved_changes_timer()

    def switch_account_status_page(self):
        self.stackedLayout.setCurrentIndex(self.comboBox.currentIndex())

    def client_version_check(self):
        """
        Compare installed version of OneDrive client with the latest version on Github releases.
        Store results and call display_version_warnings() to show combined warnings.
        """
        s = requests.Session()
        version_label_text = ""
        version_tooltip_text = ""
        min_requirements_met = True
        min_supported_version = 20511  # Enforce minimum version of onedrive client to be 2.5.11, required for the in-browser login flow support added in GUI v1.3.2

        def get_latest_client_version():
            latest_url = "https://api.github.com/repos/abraunegg/onedrive/releases/latest"
            try:
                latest_client_version = s.get(latest_url, timeout=1).json()["tag_name"]
                return latest_client_version

            except Exception as e:
                logging.error(e)
                return None

        def get_installed_client_version():
            try:
                client_version_check = subprocess.check_output([client_bin_path, "--version"], stderr=subprocess.STDOUT)
                installed_client_version = re.search(r"(v[0-9.]+)", str(client_version_check)).group(1)

                # Zero-pad each component (e.g. "2.5.9" -> "020509") so patch versions
                # compare correctly regardless of digit count, instead of comparing the
                # raw concatenated digits (which put "2.5.9" above "2.5.11").
                version_parts = installed_client_version.replace("v", "").split(".")
                installed_client_version_num = int("".join(part.zfill(2) for part in version_parts))

                return installed_client_version, installed_client_version_num, client_version_check.decode(errors="replace").strip()

            except Exception as e:
                logging.error(e)
                return None

        try:
            installed_client_version = get_installed_client_version()

            # A development build of the client (git describe suffix) or the Files On-Demand fork
            # (seen on D-Bus) is not an abraunegg release: do not compare it with the latest release.
            fork_detected = bool(self.dbus) and any(
                instance.props.get("OnDemand") or instance.has_capability("ondemand") for instance in self.dbus.instances.values()
            )
            development_label = development_build_label(installed_client_version[2], fork_detected) if installed_client_version else None
            self.client_version_status["info_text"] = ""
            latest_client_version = None if development_label else get_latest_client_version()

            if installed_client_version:
                logging.info(f"[GUI] Client version check: Installed: {installed_client_version[2]} | Latest: {latest_client_version or 'not checked'}")

            if not installed_client_version:
                version_label_text = "OneDrive client not found!"
                version_tooltip_text = "OneDrive client not found!"
                min_requirements_met = False

            elif development_label and installed_client_version[1] >= min_supported_version:
                self.client_version_status["info_text"] = f"OneDrive client: {development_label}"

            elif not latest_client_version and not development_label:
                version_label_text = "Unable to check for latest OneDrive client version!"
                version_tooltip_text = "Unable to check for latest OneDrive client version!"

            elif installed_client_version[1] < min_supported_version:
                version_label_text = (
                    'Unsupported OneDrive client! Please <a href="https://github.com/abraunegg/onedrive/blob/master/docs/install.md" style="color:#FFFFFF;">upgrade</a> it.'
                )
                version_tooltip_text = (
                    f"Your OneDrive Client version not supported! Please upgrade it. \n Installed: {installed_client_version[0]} \n Latest: {latest_client_version}"
                )
                min_requirements_met = False

            elif latest_client_version not in installed_client_version[0]:
                version_label_text = "OneDrive client is out of date!"
                version_tooltip_text = f"OneDrive client is out of date! \n Installed: {installed_client_version[0]} \n Latest: {latest_client_version}"

            # Store results
            self.client_version_status["label_text"] = version_label_text
            self.client_version_status["tooltip_text"] = version_tooltip_text
            self.client_version_status["min_requirements_met"] = min_requirements_met

            # Display combined warnings
            self.display_version_warnings()

        except Exception as e:
            logging.error(f"Client version check failed: {e}")

    def gui_version_check(self):
        """
        Compare installed version of OneDriveGUI with the latest version on Github releases.
        Store results and call display_version_warnings() to show combined warnings.
        """
        s = requests.Session()
        version_label_text = ""
        version_tooltip_text = ""

        def parse_semantic_version(version_string):
            """Parse semantic version string (e.g., 'v1.2.3' or '1.2.3') into tuple (1, 2, 3)"""
            try:
                # Remove 'v' prefix if present
                clean_version = version_string.strip().lstrip("v")
                # Split by '.' and convert to integers
                parts = [int(x) for x in clean_version.split(".")]
                # Ensure we have at least 3 parts (major, minor, patch)
                while len(parts) < 3:
                    parts.append(0)
                return tuple(parts[:3])  # Return only first 3 parts
            except Exception as e:
                logging.error(f"Failed to parse version '{version_string}': {e}")
                return None

        def get_latest_gui_version():
            """Fetch latest GUI version from GitHub releases"""
            latest_url = "https://api.github.com/repos/bpozdena/OneDriveGUI/releases/latest"
            try:
                response = s.get(latest_url, timeout=1)
                latest_gui_version = response.json()["tag_name"]
                return latest_gui_version
            except Exception as e:
                logging.error(f"Failed to fetch latest GUI version: {e}")
                return None

        try:
            latest_gui_version = get_latest_gui_version()
            installed_gui_version = version  # From options.py

            if not latest_gui_version:
                # Network issue or API error - don't show warning
                logging.warning("[GUI] Unable to check for latest OneDriveGUI version")
                return

            # Parse versions for comparison
            installed_version_tuple = parse_semantic_version(installed_gui_version)
            latest_version_tuple = parse_semantic_version(latest_gui_version)

            if not installed_version_tuple or not latest_version_tuple:
                logging.error("[GUI] Failed to parse GUI versions for comparison")
                return

            logging.info(f"[GUI] GUI version check: Installed: v{installed_gui_version} | Latest: {latest_gui_version}")

            # Compare versions
            if installed_version_tuple < latest_version_tuple:
                version_label_text = "OneDriveGUI is out of date!"
                version_tooltip_text = f"OneDriveGUI is out of date! \n Installed: v{installed_gui_version} \n Latest: {latest_gui_version}"
            else:
                logging.info("[GUI] OneDriveGUI is up to date")

            # Store results
            self.gui_version_status["label_text"] = version_label_text
            self.gui_version_status["tooltip_text"] = version_tooltip_text

            # Display combined warnings
            self.display_version_warnings()

        except Exception as e:
            logging.error(f"GUI version check failed: {e}")

    def display_version_warnings(self):
        """
        Display combined version warnings for both OneDrive client and GUI.
        This method combines warnings from both version checks and displays them together.
        """
        pixmap_warning = QPixmap(DIR_PATH + "/resources/images/warning.png").scaled(20, 20, Qt.KeepAspectRatio)

        # Combine label texts (use <br> for HTML line break in Qt labels)
        label_texts = []
        if self.client_version_status["label_text"]:
            label_texts.append(self.client_version_status["label_text"])
        if self.gui_version_status["label_text"]:
            label_texts.append(self.gui_version_status["label_text"])

        combined_label_text = "<br>".join(label_texts) if label_texts else ""

        # Combine tooltip texts
        tooltip_parts = []
        if self.client_version_status["tooltip_text"]:
            tooltip_parts.append(self.client_version_status["tooltip_text"])
        if self.gui_version_status["tooltip_text"]:
            tooltip_parts.append(self.gui_version_status["tooltip_text"])

        combined_tooltip_text = "\n---\n".join(tooltip_parts) if tooltip_parts else ""

        # Display warnings in all profile status pages
        for profile_name in global_config:
            page = self.profile_status_pages[profile_name]
            page.label_onedrive_status.setOpenExternalLinks(True)

            # Set combined label text
            if combined_label_text:
                page.label_onedrive_status.setText(combined_label_text)

            # Handle critical client version issues (disable sync)
            if not self.client_version_status["min_requirements_met"]:
                page.pushButton_start.setEnabled(False)
                page.pushButton_start_stop.setEnabled(False)
                page.pushButton_profiles.setEnabled(False)

            # Set warning icon and tooltip if any warnings exist
            if combined_tooltip_text:
                page.label_version_check.setToolTip(combined_tooltip_text)
                page.label_version_check.setPixmap(pixmap_warning)
            elif self.client_version_status.get("info_text"):
                page.label_version_check.setToolTip(self.client_version_status["info_text"])
                page.label_version_check.setPixmap(
                    themed_icon(["dialog-information-symbolic", "dialog-information"], DIR_PATH + "/resources/images/warning.png").pixmap(20, 20)
                )

    def onedrive_process_status(self):
        # Check OneDrive status and start/stop sync button.
        pixmap_running = QPixmap(DIR_PATH + "/resources/images/icons8-green-circle-48.png").scaled(24, 24, Qt.KeepAspectRatio)
        pixmap_stopped = QPixmap(DIR_PATH + "/resources/images/icons8-red-circle-48.png").scaled(24, 24, Qt.KeepAspectRatio)

        for profile_name in global_config:
            # Check if profile exists in profile_status_pages, if not add it
            if profile_name not in self.profile_status_pages:
                logging.info(f"Profile {profile_name} found in global_config but not in UI, adding it now")
                self.add_profile(profile_name)

        self.match_dbus_instances_if_profiles_changed()
        self.refresh_mode_indicators()

        for profile_name in global_config:

            profile_status_page = self.profile_status_pages[profile_name]

            if profile_name in self.attached and profile_name not in workers:
                self.update_attached_controls(profile_name, pixmap_running)

            elif profile_name not in workers:
                profile_status_page.label_status.setText("stopped")
                profile_status_page.label_status.setToolTip("Sync is stopped")
                profile_status_page.label_status.setPixmap(pixmap_stopped)

                # Show Play icon when sync is stopped.
                profile_status_page.pushButton_start_stop.setIcon(profile_status_page.start_icon)
                profile_status_page.pushButton_start_stop.setToolTip("Start Sync")
                profile_status_page.pushButton_start_stop.clicked.disconnect()
                profile_status_page.pushButton_start_stop.clicked.connect(profile_status_page.start_monitor)

            else:
                if workers[profile_name].isRunning():
                    profile_status_page.label_status.setText("running")
                    profile_status_page.label_status.setToolTip("Sync is running")
                    profile_status_page.label_status.setPixmap(pixmap_running)

                    # Show Stop icon when sync is running.
                    profile_status_page.pushButton_start_stop.setIcon(profile_status_page.stop_icon)
                    profile_status_page.pushButton_start_stop.setToolTip("Stop Sync")
                    profile_status_page.pushButton_start_stop.clicked.disconnect()
                    profile_status_page.pushButton_start_stop.clicked.connect(profile_status_page.stop_monitor)

        # Update system tray icon based on aggregate status
        self.update_tray_icon()

    def is_ignorable_error(self, error_text):
        """
        Check if an error message should be ignored for tray icon status.
        Returns True if the error should be ignored.
        """
        if not error_text:
            return True

        # List of error patterns to ignore
        ignored_patterns = ["inotify_add_watch failed", "The local file system returned an error with the following message"]

        error_lower = error_text.lower()
        for pattern in ignored_patterns:
            if pattern.lower() in error_lower:
                return True

        return False

    def aggregate_tray_status(self):
        """
        Aggregate status across all profiles to determine system tray icon state.
        Returns: tuple (status_state, running_count, total_count, profile_statuses)

        status_state: 'ERROR', 'STOPPED', 'SYNCING', or 'IDLE'
        Priority: ERROR > STOPPED > SYNCING > IDLE
        """
        if not global_config:
            return ("IDLE", 0, 0, {})

        total_count = len(global_config)
        running_count = 0
        profile_statuses = {}

        has_error = False
        has_stopped = False
        has_syncing = False

        for profile_name in global_config:
            # Check if worker is running
            is_running = profile_name in workers and workers[profile_name].isRunning()

            # Get status message from profile status page
            if profile_name in self.profile_status_pages:
                status_msg = self.profile_status_pages[profile_name].label_onedrive_status.text()
                error_tooltip = self.profile_status_pages[profile_name].label_error_icon.toolTip()
            else:
                status_msg = ""
                error_tooltip = ""

            # Categorize this profile's state
            if not is_running:
                profile_state = "STOPPED"
                has_stopped = True
            elif (not self.is_ignorable_error(error_tooltip) and not self.is_ignorable_error(status_msg)) and (
                error_tooltip or "Error:" in status_msg or "error" in status_msg.lower() or "failed" in status_msg.lower() or "crashed" in status_msg.lower()
            ):
                profile_state = "ERROR"
                has_error = True
                running_count += 1
            elif "sync in progress" in status_msg.lower() or "processing" in status_msg.lower() or "starting" in status_msg.lower() or "initializing" in status_msg.lower():
                profile_state = "SYNCING"
                has_syncing = True
                running_count += 1
            elif is_running:
                profile_state = "IDLE"
                running_count += 1
            else:
                profile_state = "STOPPED"
                has_stopped = True

            profile_statuses[profile_name] = {"state": profile_state, "status_msg": status_msg if status_msg else "stopped", "is_running": is_running}

        # Determine overall state with priority
        if has_error:
            overall_state = "ERROR"
        elif has_stopped:
            overall_state = "STOPPED"
        elif has_syncing:
            overall_state = "SYNCING"
        else:
            overall_state = "IDLE"

        return (overall_state, running_count, total_count, profile_statuses)

    def generate_tray_tooltip(self, overall_state, running_count, total_count, profile_statuses):
        """
        Generate detailed tooltip showing status of all profiles.
        """
        if not profile_statuses:
            return "OneDriveGUI - No profiles configured"

        # Summary line
        if running_count == total_count and overall_state != "ERROR":
            summary = f"All {total_count} profile(s) running"
        elif running_count == 0:
            summary = f"All {total_count} profile(s) stopped"
        else:
            summary = f"{running_count} of {total_count} profile(s) running"

        # Add overall state indicator
        state_indicators = {"ERROR": "Sync error detected", "STOPPED": "Sync stopped", "SYNCING": "Syncing...", "IDLE": "All syncs complete"}
        summary += f" - {state_indicators.get(overall_state, '')}"

        # Build detailed list
        lines = [summary, ""]
        for profile_name, status_info in profile_statuses.items():
            state = status_info["state"]
            status_msg = status_info["status_msg"]

            # Add text indicator for each profile
            if state == "ERROR":
                indicator = "[ERROR]"
            elif state == "STOPPED":
                indicator = "[STOPPED]"
            elif state == "SYNCING":
                indicator = "[SYNCING]"
            else:
                indicator = "[IDLE]"

            # Shorten status message if too long
            if len(status_msg) > 50:
                status_msg = status_msg[:47] + "..."

            lines.append(f"{indicator} {profile_name}: {status_msg}")

        return "\n".join(lines)

    def update_tray_icon(self):
        """
        Update system tray icon and tooltip based on current sync status.
        """
        if not self.tray:
            return

        if self.attached:
            self.update_attached_tray_icon()
            return

        # Get aggregated status
        overall_state, running_count, total_count, profile_statuses = self.aggregate_tray_status()

        # Select icon based on state
        icon_map = {
            "ERROR": DIR_PATH + "/resources/images/icons8-cloud-error-80.png",
            "STOPPED": DIR_PATH + "/resources/images/icons8-cloud-stop-80.png",
            "SYNCING": DIR_PATH + "/resources/images/icons8-cloud-sync-80.png",
            "IDLE": DIR_PATH + "/resources/images/icons8-cloud-done-80.png",
        }

        icon_path = icon_map.get(overall_state, DIR_PATH + "/resources/images/icons8-cloud-80.png")
        icon = QIcon(icon_path)
        tooltip = self.generate_tray_tooltip(overall_state, running_count, total_count, profile_statuses)

        if self.tray.toolTip() != tooltip:
            logging.debug("Updating tray icon and tooltip")
            self.tray.setIcon(icon)
            self.tray.setToolTip(tooltip)

    def autostart_monitor(self):
        # Auto-start sync if compatible version of OneDrive client is installed.
        for profile_name in global_config:
            logging.info(f"[{profile_name}] Compatible client version found: {self.profile_status_pages[profile_name].pushButton_start.isEnabled()}")
            logging.info(f"[{profile_name}] Auto-sync enabled for profile: {global_config[profile_name]['auto_sync']}")

            if self.profile_status_pages[profile_name].pushButton_start.isEnabled() and global_config[profile_name]["auto_sync"] == "True":
                decision = start_decision(global_config[profile_name], profile_name in self.attached, profile_name in workers)
                if decision != "spawn":
                    logging.info(f"[{profile_name}] Not starting a client at GUI start: {decision}")
                    continue
                self.start_onedrive_monitor(profile_name)

    def start_onedrive_monitor(self, profile_name, options=""):
        decision = start_decision(global_config[profile_name], profile_name in self.attached, profile_name in workers)
        if decision == "attach":
            # Never start a second client next to one that runs outside the GUI.
            logging.info(f"[{profile_name}] A client is already running on D-Bus, attaching instead of starting one")
            return
        if decision == "service":
            self.offer_start_service(profile_name)
            return

        # Clear any previous error messages when starting sync
        self.profile_status_pages[profile_name].label_error_icon.clear()
        self.profile_status_pages[profile_name].label_error_icon.setToolTip("")

        if profile_name not in workers:
            workers[profile_name] = WorkerThread(profile_name, options)
            workers[profile_name].start()
        else:
            logging.warning(f"Worker for profile {profile_name} is already running. Please stop it first.")
            logging.info(f"Running workers: {workers}")

        if "True" not in gui_settings.get("QWebEngine_login"):
            # Assume GUI runs as AppImage. Force login in external browser as a workaround for #37 .
            logging.info(f"[GUI] Opening external login window")
            workers[profile_name].update_credentials.connect(self.show_external_login)
        else:
            # Use QT WebEngine for a more convenient login.
            logging.info(f"[GUI] Opening WebEngine login window")
            workers[profile_name].update_credentials.connect(self.show_login)

        workers[profile_name].trigger_resync.connect(self.resync_auth_dialog)
        workers[profile_name].trigger_big_delete.connect(self.big_delete_auth_dialog)
        workers[profile_name].browser_login_required.connect(self.show_browser_login_notification)
        workers[profile_name].update_progress_new.connect(self.event_update_progress)
        workers[profile_name].update_profile_status.connect(self.event_update_profile_status)
        workers[profile_name].clear_warning.connect(self.clear_warning_handler)
        workers[profile_name].started.connect(lambda: logging.info(f"started worker {profile_name}"))
        workers[profile_name].finished.connect(lambda: logging.info(f"finished worker {profile_name}"))
        try:
            workers[profile_name].finished.connect(lambda: self.remove_worker(profile_name))
        except KeyError:
            logging.info(f"[GUI] The worker for profile {profile_name} is already stopped.")

    def show_browser_login_notification(self, profile_name):
        if self.tray:
            self.tray.showMessage(
                "OneDriveGUI - Login required",
                f"Please complete the OneDrive login for '{profile_name}' in the web browser window that just opened.",
                QSystemTrayIcon.Information,
                10000,
            )

    def resync_auth_dialog(self, profile_name):
        resync_question = QMessageBox(
            QMessageBox.Question,
            f"Resync required for profile {profile_name}",
            "An application configuration change has been detected where a resync is required. <br><br>"
            "The use of resync will remove your local 'onedrive' client state, thus no record will exist regarding your current 'sync status'. <br><br>"
            "This has the potential to overwrite local versions of files with potentially older versions downloaded from OneDrive which can lead to <b>data loss</b>. <br><br>"
            "If in-doubt, backup your local data first before proceeding with resync.<br><br><br>"
            f"Would you like to perform resync for profile <b>{profile_name}</b>?",
            QMessageBox.Yes | QMessageBox.No,
        )
        yes_button = resync_question.button(QMessageBox.Yes)
        yes_button.setEnabled(False)
        self.countdown_timer = QTimer(self)
        self.countdown_value = 5

        def update_button_text():
            yes_button.setText(f"Yes ({self.countdown_value})")
            self.countdown_value -= 1
            if self.countdown_value < 0:
                self.countdown_timer.stop()
                yes_button.setText("Yes")
                yes_button.setEnabled(True)

        self.countdown_timer.timeout.connect(update_button_text)
        self.countdown_timer.start(1000)  # Update every 1 second

        resync_question.setDefaultButton(QMessageBox.No)
        result = resync_question.exec()

        if result == QMessageBox.Yes:
            logging.info("Authorize sync: Yes")
            self.profile_status_pages[profile_name].stop_monitor()
            self.remove_worker(profile_name)
            self.start_onedrive_monitor(profile_name, "--resync --resync-auth")

        elif result == QMessageBox.No:
            logging.info("Authorize sync: No")
            self.countdown_timer.stop()
            self.profile_status_pages[profile_name].stop_monitor()

    def big_delete_auth_dialog(self, profile_name):
        big_delete_question = QMessageBox.question(
            self,
            f"Big Delete detected for profile {profile_name}",
            "An attempt to remove a large volume of data from OneDrive has been detected. <br><br>"
            "This has the potential to delete a large volume of data from your OneDrive which can lead to <b>data loss</b>. <br><br>"
            "If in-doubt, backup your OneDrive data first before proceeding with big delete.<br><br><br>"
            f"Would you like to proceed with big delete for profile <b>{profile_name}</b>?",
            buttons=QMessageBox.Yes | QMessageBox.No,
            defaultButton=QMessageBox.No,
        )

        if big_delete_question == QMessageBox.Yes:
            logging.info("Authorize big delete: Yes")
            self.profile_status_pages[profile_name].stop_monitor()
            self.remove_worker(profile_name)
            self.start_onedrive_monitor(profile_name, "--force")
            self.profile_status_pages[profile_name].label_onedrive_status.setText("Big delete approved")

        elif big_delete_question == QMessageBox.No:
            logging.info("Authorize big delete: No")
            self.profile_status_pages[profile_name].stop_monitor()
            self.profile_status_pages[profile_name].label_onedrive_status.setText("Big delete denied")

    def event_update_profile_status(self, data, profile):
        self.profile_status_pages[profile].label_onedrive_status.setText(data["status_message"])
        self.profile_status_pages[profile].label_free_space.setText(data["free_space"])
        self.profile_status_pages[profile].label_account_type.setText(data["account_type"])

        # Persist the account type reported by the client so it survives a
        # restart; the worker only updates the in-memory config. Saving here
        # keeps config writes on the main thread.
        if data["account_type"] and data["account_type"] != self.saved_account_types.get(profile):
            self.saved_account_types[profile] = data["account_type"]
            save_global_config(global_config)
            logging.info(f"[{profile}] Saved account type: {data['account_type']}")

        # Handle error message display
        if "error_message" in data and data["error_message"]:
            # Show error icon with full error message in tooltip
            pixmap_warning = QPixmap(DIR_PATH + "/resources/images/warning.png").scaled(20, 20, Qt.KeepAspectRatio)
            self.profile_status_pages[profile].label_error_icon.setPixmap(pixmap_warning)
            self.profile_status_pages[profile].label_error_icon.setToolTip(data["error_message"])
        else:
            # Clear error icon
            self.profile_status_pages[profile].label_error_icon.clear()
            self.profile_status_pages[profile].label_error_icon.setToolTip("")

    def clear_warning_handler(self, profile_name):
        """Clear warning icon and tooltip for a profile when a new sync cycle starts."""
        self.profile_status_pages[profile_name].label_error_icon.clear()
        self.profile_status_pages[profile_name].label_error_icon.setToolTip("")
        # Update tray icon to reflect the cleared warning state
        self.update_tray_icon()
        logging.debug(f"[{profile_name}] Cleared warning state before or after new sync cycle")

    def event_update_progress(self, data, profile):
        """
        data:
        transfer_progress = {
            "file_operation": file_operation.group(1),
            "file_path": file_path.group(1),
            "progress": progress.group(1),
            "transfer_complete": transfer_complete
        }
        """
        _sync_dir = os.path.expanduser(global_config[profile]["onedrive"]["sync_dir"].strip('"'))

        logging.debug(data)
        file_path = f"{_sync_dir}" + "/" + data["file_path"]
        absolute_path = QFileInfo(file_path).absolutePath().replace(" ", "%20")
        relative_path_display = os.path.relpath(QFileInfo(file_path).absolutePath(), _sync_dir + os.path.sep)
        parent_dir = re.search(r".+/([^/]+)/.+$", file_path).group(1)
        file_size = QFileInfo(file_path + ".partial").size() if QFileInfo(file_path).size() == 0 else QFileInfo(file_path).size()
        file_size_human = humanize_file_size(file_size)
        file_name = QFileInfo(file_path).fileName()
        progress = data["progress"]
        progress_data = file_size / 100 * int(progress)
        progress_data_human = humanize_file_size(progress_data)
        file_operation = data["file_operation"]
        transfer_complete = data["transfer_complete"]
        move_scrollbar = True
        new_list_item = True

        for row in range(50):
            # Check last 50 entries to see if the file is already in the list.
            if self.profile_status_pages[profile].listWidget.item(row) != None:
                item = self.profile_status_pages[profile].listWidget.item(row)
                item_widget = self.profile_status_pages[profile].listWidget.itemWidget(item)
                item_file_name = item_widget.get_file_name()
                item_incomplete = item_widget.get_timestamp_text() == ""

                if file_name == item_file_name and item_incomplete:
                    # If current file is already in the list and progress bar is not complete, update the existing item.
                    # Otherwise skip and create a new item.
                    logging.debug("Updating list item")

                    item_widget.set_progress(int(progress))
                    item_widget.set_icon(file_path)
                    item_widget.hide_progress_bar(transfer_complete)

                    if file_operation == "Deleting":
                        item_widget.set_label_1(f"Deleted from {parent_dir}")
                        item_widget.set_custom_icon(self.trash_icon)
                        item_widget.set_label_2(f"")
                        # Store timestamp and display for deleted files
                        if "timestamp" in data and data["timestamp"]:
                            item_widget.set_completion_timestamp(data["timestamp"])
                            relative_time = format_relative_time(data["timestamp"])
                            item_widget.set_timestamp(relative_time)
                        else:
                            item_widget.set_timestamp("")

                    elif file_operation == "Moving":
                        item_widget.set_label_1(f"Trashed from {parent_dir}")
                        item_widget.set_label_2(f"")
                        # Store timestamp and display for moved files
                        if "timestamp" in data and data["timestamp"]:
                            item_widget.set_completion_timestamp(data["timestamp"])
                            relative_time = format_relative_time(data["timestamp"])
                            item_widget.set_timestamp(relative_time)
                        else:
                            item_widget.set_timestamp("")

                    elif transfer_complete and file_operation == "Uploading":
                        shortened_path = shorten_path(relative_path_display, 32)
                        item_widget.set_label_1(f"Uploaded from <a href=file:///{absolute_path}>{shortened_path}</a>")
                        item_widget.set_label_2(f"{file_size_human}")
                        # Store timestamp and display in separate timestamp label
                        if "timestamp" in data and data["timestamp"]:
                            item_widget.set_completion_timestamp(data["timestamp"])
                            relative_time = format_relative_time(data["timestamp"])
                            item_widget.set_timestamp(relative_time)
                        else:
                            item_widget.set_timestamp("")

                    elif transfer_complete and file_operation == "Downloading":
                        shortened_path = shorten_path(relative_path_display, 32)
                        item_widget.set_label_1(f"Downloaded from <a href=file:///{absolute_path}>{shortened_path}</a>")
                        item_widget.set_label_2(f"{file_size_human}")
                        # Store timestamp and display in separate timestamp label
                        if "timestamp" in data and data["timestamp"]:
                            item_widget.set_completion_timestamp(data["timestamp"])
                            relative_time = format_relative_time(data["timestamp"])
                            item_widget.set_timestamp(relative_time)
                        else:
                            item_widget.set_timestamp("")

                    elif file_operation == "Downloading":
                        # Estimate final size of file before download completes
                        # Adding 5% to progress as the OD client report status 5% behind.
                        item_widget.set_label_1(file_operation)
                        item_widget.set_label_2(f"{humanize_file_size(file_size)} of ~{humanize_file_size(int(file_size) / (int(progress) + 5) * 100)}")
                    else:
                        item_widget.set_label_1(file_operation)
                        item_widget.set_label_2(f"{progress_data_human} of {file_size_human}")

                    self.profile_status_pages[profile].listWidget.setItemWidget(item, item_widget)
                    new_list_item = False
                    logging.debug(f"List item updated for file {item_file_name}")
                    break

        if new_list_item:
            logging.debug(f"Adding new list item for file {file_name}")
            myQCustomQWidget = TaskList()
            myQCustomQWidget.set_file_name(file_name)
            myQCustomQWidget.set_progress(int(progress))
            myQCustomQWidget.set_icon(file_path)
            myQCustomQWidget.hide_progress_bar(transfer_complete)

            if file_operation == "Deleting":
                myQCustomQWidget.set_label_1(f"Deleted from {parent_dir}")
                myQCustomQWidget.set_custom_icon(self.trash_icon)
                myQCustomQWidget.set_label_2(f"")
                # Store timestamp and display for deleted files
                if "timestamp" in data and data["timestamp"]:
                    myQCustomQWidget.set_completion_timestamp(data["timestamp"])
                    relative_time = format_relative_time(data["timestamp"])
                    myQCustomQWidget.set_timestamp(relative_time)
                else:
                    myQCustomQWidget.set_timestamp("")

            elif file_operation == "Moving":
                myQCustomQWidget.set_label_1(f"Trashed from {parent_dir}")
                myQCustomQWidget.set_label_2(f"")
                # Store timestamp and display for moved files
                if "timestamp" in data and data["timestamp"]:
                    myQCustomQWidget.set_completion_timestamp(data["timestamp"])
                    relative_time = format_relative_time(data["timestamp"])
                    myQCustomQWidget.set_timestamp(relative_time)
                else:
                    myQCustomQWidget.set_timestamp("")

            elif transfer_complete and file_operation == "Uploading":
                shortened_path = shorten_path(relative_path_display, 32)
                myQCustomQWidget.set_label_1(f"Uploaded from <a href=file:///{absolute_path}>{shortened_path}</a>")
                myQCustomQWidget.set_label_2(f"{file_size_human}")
                # Store timestamp and display in separate timestamp label
                if "timestamp" in data and data["timestamp"]:
                    myQCustomQWidget.set_completion_timestamp(data["timestamp"])
                    relative_time = format_relative_time(data["timestamp"])
                    myQCustomQWidget.set_timestamp(relative_time)
                else:
                    myQCustomQWidget.set_timestamp("")

            elif transfer_complete and file_operation == "Downloading":
                shortened_path = shorten_path(relative_path_display, 32)
                myQCustomQWidget.set_label_1(f"Downloaded from <a href=file:///{absolute_path}>{shortened_path}</a>")
                myQCustomQWidget.set_label_2(f"{file_size_human}")
                # Store timestamp and display in separate timestamp label
                if "timestamp" in data and data["timestamp"]:
                    myQCustomQWidget.set_completion_timestamp(data["timestamp"])
                    relative_time = format_relative_time(data["timestamp"])
                    myQCustomQWidget.set_timestamp(relative_time)
                else:
                    myQCustomQWidget.set_timestamp("")

            elif file_operation == "Downloading":
                # Estimate final size of file before download completes
                # Adding 5% to progress as the OD client report status 5% behind.
                myQCustomQWidget.set_label_1(file_operation)
                myQCustomQWidget.set_label_2(f"{humanize_file_size(file_size)} of ~{humanize_file_size(int(file_size) / (int(progress) + 5) * 100)}")
            else:
                myQCustomQWidget.set_label_1(file_operation)
                myQCustomQWidget.set_label_2(f"{progress_data_human} of {file_size_human}")

            # Create QListWidgetItem
            myQListWidgetItem = QListWidgetItem()

            # Set size hint
            myQListWidgetItem.setSizeHint(myQCustomQWidget.sizeHint())

            # Add QListWidgetItem into QListWidget
            listWidget = self.profile_status_pages[profile].listWidget
            listWidget.insertItem(0, myQListWidgetItem)
            listWidget.setItemWidget(myQListWidgetItem, myQCustomQWidget)

            # If the list is not scrolled all the way to the top, increment the scroll value so that the list stays in the same spot when new items are added.
            scroll = listWidget.verticalScrollBar()
            currentScrollValue = scroll.value()
            if move_scrollbar and (currentScrollValue > 0):
                scroll.setValue(currentScrollValue + 1)

            # Limit list to 10k items to fix #208.
            if listWidget.count() > 10_000:
                listWidget.takeItem(listWidget.count() - 1)

    def on_list_scrolled(self, value):
        """
        Debounced scroll handler - triggers timestamp update after scrolling stops.
        Called when scrollbar position changes (user scroll or automatic scroll from new items).
        """
        # Restart the debounce timer - only triggers after 200ms of no scrolling
        self.scroll_update_timer.start()

    def update_relative_times(self):
        """
        Update relative time display only for visible items in the viewport.
        Optimized to update only 10-20 visible items instead of all 10,000 items.
        Called every 60 seconds by QTimer and when user scrolls the list.
        """
        for profile in self.profile_status_pages:
            listWidget = self.profile_status_pages[profile].listWidget

            # Get the visible viewport area
            rect = listWidget.viewport().contentsRect()
            top_index = listWidget.indexAt(rect.topLeft())

            # Skip if no items are visible
            if not top_index.isValid():
                continue

            # Get the bottom visible index
            bottom_index = listWidget.indexAt(rect.bottomLeft())

            # Handle case where bottom index is invalid (last item is visible)
            if not bottom_index.isValid():
                bottom_row = listWidget.count() - 1
            else:
                bottom_row = bottom_index.row()

            # Only iterate through visible rows (typically 10-20 items instead of 10,000)
            for row in range(top_index.row(), bottom_row + 1):
                item = listWidget.item(row)
                if item is not None:
                    item_widget = listWidget.itemWidget(item)
                    if item_widget is not None:
                        # Get the completion timestamp
                        timestamp = item_widget.get_completion_timestamp()

                        # If item has a timestamp, update the relative time in the timestamp label
                        if timestamp is not None:
                            relative_time = format_relative_time(timestamp)
                            item_widget.set_timestamp(relative_time)

    def _build_login_url(self, profile):
        application_id = global_config[profile]["onedrive"]["application_id"].strip('"') or "d50ca740-c83f-4d1b-b616-12c519384f0c"
        azure_tenant_id = global_config[profile]["onedrive"]["azure_tenant_id"].strip('"') or "common"
        self.login_url = (
            f"https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/authorize"
            f"?client_id={application_id}"
            f"&scope=Files.ReadWrite%20Files.ReadWrite.All%20Sites.ReadWrite.All%20offline_access"
            f"&response_type=code&prompt=login"
            f"&redirect_uri=https://login.microsoftonline.com/{azure_tenant_id}/oauth2/nativeclient"
        )
        logging.debug(f"[GUI] Built login URL: {self.login_url}")

    def show_login(self, profile):
        # Show login window with QT WebEngine
        self.window1 = QWidget()
        self.window1.setWindowIcon(QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png"))
        self.lw = Ui_LoginWindow()
        self.lw.setupUi(self.window1)
        self.window1.show()
        self.window1.setWindowTitle(f"OneDrive login for profile {profile}")

        self.config_file = global_config[profile]["config_file"].strip('"')
        self.config_dir = re.search(r"(.+)/.+$", self.config_file).group(1)

        # Build login URL from profile config
        self._build_login_url(profile)
        self.lw.loginFrame.setUrl(QUrl(self.login_url))

        # Wait for user to login and obtain response URL
        self.lw.loginFrame.urlChanged.connect(lambda: self.get_response_url(self.lw.loginFrame.url().toString(), profile))

    def show_external_login(self, profile):
        # Show external login window
        self.window2 = QWidget()
        self.window2.setWindowIcon(QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png"))
        self.lw2 = Ui_ExternalLoginWindow()
        self.lw2.setupUi(self.window2)
        self.window2.show()
        self.window2.setWindowTitle(f"OneDrive login for profile {profile}")

        self.config_file = global_config[profile]["config_file"].strip('"')
        self.config_dir = re.search(r"(.+)/.+$", self.config_file).group(1)

        # Build login URL from profile config and update the label
        self._build_login_url(profile)
        self.lw2.label_2.setText(
            f"<html><head/><body><p>1)Login to OneDrive in your browser by "
            f'<a href="{self.login_url}"><span style=" text-decoration: underline; color:#5e81ac;">clicking this link.</span></a></p>'
            f"<p>2)Copy the response URI from your browser's address bar to the below field. </p>"
            f"<p>3)Press Save.</p></body></html>"
        )

        self.lw2.label_2.setOpenExternalLinks(True)
        self.lw2.pushButton_login.setEnabled(False)

        self.lw2.lineEdit_response_url.textChanged.connect(self.enable_login_button)

        self.lw2.pushButton_login.clicked.connect(lambda: self.get_response_url(self.lw2.lineEdit_response_url.text(), profile))

    def enable_login_button(self):
        # Enable 'Save' button only when valid URL with login response code is provided.
        if "nativeclient?code=" in self.lw2.lineEdit_response_url.text():
            logging.info(f"[GUI] Valid login response code provided. Enabling Save button.")
            self.lw2.pushButton_login.setEnabled(True)
        else:
            logging.info(f"[GUI] Invalid login response code provided. Disabling Save button.")
            self.lw2.pushButton_login.setEnabled(False)

    def get_response_url(self, response_url, profile):
        # Get response URL from OneDrive OAuth2
        if "nativeclient?code=" in response_url:
            logging.info("Login performed")

            options = f'--reauth --auth-response "{response_url}"'
            self.login = MaintenanceWorker(profile, options)
            self.login.start()
            self.login.update_login_response.connect(self.login_failed_dialog)
        else:
            pass

    def login_failed_dialog(self, response: dict):
        profile_name = response["profile_name"]
        response_reason = response["response"]

        if response_reason == "success":
            self.profile_status_pages[profile_name].label_onedrive_status.setText("Login successful. Please, start sync manually.")

            response_dialog = QMessageBox.information(
                self,
                "Login successful",
                f"Login successful!  Please start sync manually.",
                buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.Ok,
            )
        else:
            self.profile_status_pages[profile_name].label_onedrive_status.setText("Login failed.")

            response_dialog = QMessageBox.critical(
                self,
                "Login failed",
                f"Login failed!  Please verify your response URL and try again.<br><br> {response_reason}",
                buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.Ok,
            )

        if response_dialog == QMessageBox.Ok:
            logging.info("[GUI] Login response message acknowledged.")

            if "True " not in gui_settings.get("QWebEngine_login") and response_reason == "success":
                self.window2.hide()
            elif response_reason == "success":
                self.window1.hide()

            else:
                self.window2.activateWindow()
                self.window2.raise_()

    def remove_worker(self, profile_name):
        logging.info(f"[{profile_name}] Removing thread info")
        workers.pop(profile_name, None)
        logging.info(f"[GUI] Remaining running workers: {workers}")

    def add_profile(self, profile_name):
        """
        Adds a new profile to the main window UI.
        Called when a new profile is created through the wizard.
        """
        logging.info(f"[MAIN_WINDOW] Adding profile {profile_name} to main window UI")

        # Check if profile already exists in UI
        if profile_name in self.profile_status_pages:
            logging.info(f"[MAIN_WINDOW] Profile {profile_name} already exists in UI, skipping add")
            return

        # Create a profile status page and add it to the UI
        self.comboBox.addItem(profile_name)
        self.profile_status_pages[profile_name] = ProfileStatusPage(profile_name)
        self.profile_status_pages[profile_name].start_sync_signal.connect(self.start_onedrive_monitor)
        self.profile_status_pages[profile_name].stop_sync_signal.connect(self.stop_onedrive_monitor)
        self.profile_status_pages[profile_name].quit_gui_signal.connect(self.graceful_shutdown)
        self.stackedLayout.addWidget(self.profile_status_pages[profile_name])

        # Select the newly added profile
        index = self.comboBox.findText(profile_name)
        if index >= 0:
            self.comboBox.setCurrentIndex(index)
            self.stackedLayout.setCurrentIndex(index)
            logging.info(f"[MAIN_WINDOW] Selected profile {profile_name} at index {index}")

        # Show comboBox if more than one profile exists
        if len(self.profile_status_pages) > 1:
            self.comboBox.show()

        # Force update the UI
        self.comboBox.update()
        self.update()

        # Make the profile visible and force a layout refresh
        self.comboBox.show()
        self.stackedLayout.update()
        self.verticalLayout_2.update()

        # A client for the new profile may already be running (e.g. a systemd unit).
        self.match_dbus_instances()

        # Process events to ensure UI updates immediately
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.processEvents()

    @Slot(str, str)
    def rename_profile_in_main_window(self, old_name, new_name):
        """
        Updates the profile name in the main window UI elements.
        """
        logging.info(f"[MAIN_WINDOW] Renaming profile '{old_name}' to '{new_name}' in main window UI.")

        # 1. Update the item text in the comboBox.
        index = self.comboBox.findText(old_name)
        if index >= 0:
            self.comboBox.setItemText(index, new_name)
            logging.info(f"[MAIN_WINDOW] Updated comboBox item at index {index} from '{old_name}' to '{new_name}'.")
        else:
            logging.warning(f"[MAIN_WINDOW] Profile '{old_name}' not found in comboBox.")

        # 2. Update the key in the self.profile_status_pages dictionary.
        if old_name in self.profile_status_pages:
            self.profile_status_pages[new_name] = self.profile_status_pages.pop(old_name)
            # Also update the profile_name attribute within the ProfileStatusPage instance
            self.profile_status_pages[new_name].profile_name = new_name
            logging.info(f"[MAIN_WINDOW] Updated profile_status_pages key from '{old_name}' to '{new_name}'.")
        else:
            logging.warning(f"[MAIN_WINDOW] Profile '{old_name}' not found in profile_status_pages.")

        # 3. Update the key in the workers dictionary if a worker exists.
        if old_name in workers:
            workers[new_name] = workers.pop(old_name)
            # Also update the profile_name attribute within the WorkerThread instance
            workers[new_name].profile_name = new_name
            logging.info(f"[MAIN_WINDOW] Updated workers key from '{old_name}' to '{new_name}'.")
        else:
            logging.info(f"[MAIN_WINDOW] No worker found for profile '{old_name}'.")

        # 4. Re-match D-Bus clients under the new name; move an open status window along.
        if old_name in self.status_windows:
            self.status_windows[new_name] = self.status_windows.pop(old_name)
            self.status_windows[new_name].profile_name = new_name
        self.match_dbus_instances()

        # Force update the UI
        self.comboBox.update()
        self.update()

        # Process events to ensure UI updates immediately
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.processEvents()

    def remove_profile(self, profile_name):
        """
        Removes onedrive profile from the GUI.
        """
        combo_box_index = self.comboBox.findText(profile_name)
        self.comboBox.removeItem(combo_box_index)
        self.stackedLayout.setCurrentIndex(0)
        self.profile_status_pages.pop(profile_name, None)
        global_config.pop(profile_name, None)
        logging.info(global_config)

        if len(global_config) < 2:
            self.comboBox.hide()

    # --- Clients attached over D-Bus ---------------------------------------------

    def profile_has_dbus_client(self, profile_name):
        """True if a client for this profile's confdir is on the bus, even before the profile was matched."""
        if profile_name in self.attached:
            return True
        if not self.dbus or profile_name not in global_config:
            return False
        instances = {bus: instance.props for bus, instance in self.dbus.instances.items()}
        return bool(match_profiles({profile_name: profile_confdir(global_config[profile_name])}, instances))

    def match_dbus_instances_if_profiles_changed(self):
        """Re-match when a profile was added, renamed or got another config file."""
        config_files = {profile: global_config[profile].get("config_file") for profile in global_config}
        if config_files != self.matched_config_files:
            self.match_dbus_instances()

    def match_dbus_instances(self, _bus_name=None):
        """Recompute which profiles have a client on the bus (by ConfigDir)."""
        if not self.dbus:
            return
        self.matched_config_files = {profile: global_config[profile].get("config_file") for profile in global_config}
        confdirs = {profile: profile_confdir(global_config[profile]) for profile in global_config}
        attached = match_profiles(confdirs, {bus: instance.props for bus, instance in self.dbus.instances.items()})

        for profile in set(self.attached) | set(attached):
            if self.attached.get(profile) != attached.get(profile):
                logging.info(f"[{profile}] D-Bus client: {attached.get(profile) or 'gone'}")
        detached = set(self.attached) - set(attached)
        self.attached = attached

        for profile in detached:
            page = self.profile_status_pages.get(profile)
            if page:
                page.pushButton_status.hide()
                page.label_onedrive_status.setText("The OneDrive client for this profile is not running.")
        for profile in attached:
            self.update_attached_page(profile)
            self.update_attached_transfers(profile)
            if profile in self.profile_status_pages and profile not in workers:
                self.update_attached_controls(profile, self.pixmap_running())

        self.refresh_tray_menu()
        self.update_tray_icon()

    def on_dbus_instance_changed(self, bus_name):
        for profile, attached_bus in self.attached.items():
            if attached_bus == bus_name:
                self.update_attached_page(profile)
        self.refresh_tray_menu()
        self.update_tray_icon()
        self.mode_indicators_changed.emit()

    def profile_mode(self, profile_name):
        return profile_mode(global_config[profile_name], self.attached_instance(profile_name), profile_name in workers, self.unit_states)

    def profile_mode_text(self, profile_name, separator=" - "):
        if profile_name not in global_config:
            return ""
        return mode_text(self.profile_mode(profile_name), separator)

    def refresh_mode_indicators(self):
        """Files On-Demand badge and how the client runs, on every place that shows a profile."""
        texts = {profile: self.profile_mode_text(profile) for profile in global_config}
        # Every pass, not only on a text change: attaching/detaching rewrites the status line,
        # and a stopped service must still read "Stopped (background service)" afterwards.
        for profile in global_config:
            if profile in self.profile_status_pages:
                self.update_service_button(profile, self.profile_mode(profile))
        if texts == self.mode_texts:
            return
        self.mode_texts = texts

        for profile, text in texts.items():
            page = self.profile_status_pages.get(profile)
            if page is not None:
                mode = self.profile_mode(profile)
                # Short form in the header; the unit and its state are in the tooltip.
                page.label_mode.setText(mode_text(mode, include_unit=False))
                page.label_mode.setToolTip(text)
            index = self.comboBox.findText(profile)
            if index >= 0:
                self.comboBox.setItemData(index, text, Qt.ToolTipRole)
            for item in profile_settings_window.listWidget_profiles.findItems(profile, Qt.MatchExactly):
                set_profile_item_tooltip(item, mode=text)

        self.refresh_tray_menu()
        self.mode_indicators_changed.emit()

    def attached_instance(self, profile_name):
        bus_name = self.attached.get(profile_name)
        return self.dbus.instances.get(bus_name) if self.dbus and bus_name else None

    def update_attached_page(self, profile_name):
        instance = self.attached_instance(profile_name)
        page = self.profile_status_pages.get(profile_name)
        if instance is None or page is None:
            return
        props = instance.props
        page.pushButton_status.show()
        page.label_onedrive_status.setText(status_text(props, instance.issues))
        if props.get("AccountType") and props["AccountType"] != "unknown":
            page.label_account_type.setText(props["AccountType"].capitalize())
        if props.get("QuotaTotal"):
            page.label_free_space.setText(humanize_file_size(props["QuotaTotal"] - props.get("QuotaUsed", 0)))

    def on_dbus_transfers_changed(self, bus_name):
        for profile, attached_bus in self.attached.items():
            if attached_bus == bus_name:
                self.update_attached_transfers(profile)

    def update_attached_transfers(self, profile_name):
        """
        Show the transfers of an attached client in the profile's transfer list, like the
        log-parsed list of a GUI-started client. A transfer that leaves GetTransfers() is
        shown as finished, or failed if the client reports an issue for its path.
        """
        instance = self.attached_instance(profile_name)
        page = self.profile_status_pages.get(profile_name)
        if instance is None or page is None or profile_name in workers or not instance.has_capability("transfers"):
            return

        items = self.attached_transfer_items.setdefault(profile_name, {})  # path -> (widget, direction, total)
        current = {transfer[0]: transfer for transfer in instance.transfers}
        failed_paths = {issue[1] for issue in instance.issues if issue[3] == "attention"}
        finished_labels = {"upload": "Uploaded to", "download": "Downloaded to", "hydrate": "Downloaded on demand to"}

        for path in [path for path in items if path not in current]:
            widget, direction, total = items.pop(path)
            folder = os.path.dirname(path) or "OneDrive"
            if path in failed_paths:
                widget.set_label_1(f"{'Upload' if direction == 'upload' else 'Download'} failed")
            else:
                widget.set_label_1(f"{finished_labels.get(direction, 'Synced to')} {folder}")
                widget.set_label_2(humanize_file_size(total) if total else "")
                widget.set_progress(100)
            widget.hide_progress_bar(True)
            widget.set_completion_timestamp(datetime.now())
            widget.set_timestamp(format_relative_time(widget.get_completion_timestamp()))

        active_labels = {"upload": "Uploading", "download": "Downloading", "hydrate": "Downloading on demand"}
        for path, direction, state, done, total in instance.transfers:
            if path in items:
                widget = items[path][0]
            else:
                widget = TaskList()
                widget.set_file_name(os.path.basename(path))
                widget.set_custom_icon(file_type_icon(path))
                widget.set_timestamp("")
                list_item = QListWidgetItem()
                list_item.setSizeHint(widget.sizeHint())
                page.listWidget.insertItem(0, list_item)
                page.listWidget.setItemWidget(list_item, widget)
                if page.listWidget.count() > 10_000:
                    page.listWidget.takeItem(page.listWidget.count() - 1)
            items[path] = (widget, direction, total)
            widget.hide_progress_bar(False)
            widget.set_label_1("Queued" if state == "queued" else active_labels.get(direction, direction))
            widget.set_label_2(f"{humanize_file_size(done)} of {humanize_file_size(total)}" if total else "")
            widget.set_progress(int(done * 100 / total) if total else 0)

    def pixmap_running(self):
        return QPixmap(DIR_PATH + "/resources/images/icons8-green-circle-48.png").scaled(24, 24, Qt.KeepAspectRatio)

    def update_attached_controls(self, profile_name, pixmap_running):
        """Status light and start/stop button of a profile whose client runs outside the GUI."""
        page = self.profile_status_pages[profile_name]
        instance = self.attached_instance(profile_name)
        paused = instance is not None and instance.props.get("State") == "paused"

        page.label_status.setText("running")
        page.label_status.setToolTip("Client started outside OneDriveGUI (e.g. by systemd)")
        page.label_status.setPixmap(pixmap_running)

        # The GUI does not stop a client it did not start; the button pauses and resumes instead.
        page.pushButton_start_stop.clicked.disconnect()
        if instance is not None and instance.has_capability("pause"):
            page.pushButton_start_stop.setEnabled(True)
            page.pushButton_start_stop.setIcon(page.start_icon if paused else page.pause_icon)
            page.pushButton_start_stop.setToolTip("Resume syncing" if paused else "Pause syncing")
            page.pushButton_start_stop.clicked.connect(lambda: self.toggle_pause(profile_name))
        else:
            page.pushButton_start_stop.setEnabled(False)
            page.pushButton_start_stop.setIcon(page.stop_icon)
            page.pushButton_start_stop.setToolTip("Managed outside OneDriveGUI")

    def update_attached_tray_icon(self):
        _overall, _running, _total, profile_statuses = self.aggregate_tray_status()
        upstream_states = {"ERROR": "error", "STOPPED": "stopped", "SYNCING": "syncing", "IDLE": "synced"}

        states = {}
        lines = []
        for profile_name in global_config:
            instance = self.attached_instance(profile_name)
            if instance is not None:
                states[profile_name] = tray_state(instance.props, instance.issues)
                detail = status_text(instance.props, instance.issues)
            else:
                status = profile_statuses.get(profile_name, {"state": "STOPPED", "status_msg": "stopped"})
                states[profile_name] = upstream_states.get(status["state"], "stopped")
                detail = status["status_msg"]
            lines.append(f"{profile_name}: {detail}")

        overall = aggregate_tray_state(set(states.values()))
        if len(lines) == 1:
            tooltip = f"OneDrive - {lines[0].split(': ', 1)[1]}"
        else:
            tooltip = "\n".join([f"OneDrive - {STATE_LABELS.get(overall, overall)}", ""] + lines)

        if self.tray.toolTip() != tooltip or getattr(self, "tray_overall_state", None) != overall:
            self.tray_overall_state = overall
            self.tray.setIcon(tray_icon(overall))
            self.tray.setToolTip(tooltip)

    def update_service_button(self, profile_name, mode):
        """Start/Stop background service on the profile's main page; the status line when it is stopped."""
        page = self.profile_status_pages[profile_name]
        unit = mode["unit"]
        page.pushButton_service.setVisible(bool(unit))
        if not unit:
            return
        running = mode["active"] in ACTIVE_STATES
        page.pushButton_service.setText("Stop background service" if running else "Start background service")
        page.pushButton_service.setToolTip(f"{unit}: {mode['active']}")
        if profile_name not in self.attached and profile_name not in workers and profile_name not in self.service_actions:
            if mode["active"] in ("inactive", "failed", "deactivating"):
                page.label_onedrive_status.setText("Stopped (background service)" if mode["active"] != "failed" else f"Background service failed ({unit})")
            elif mode["active"] in ACTIVE_STATES:
                page.label_onedrive_status.setText("Starting background service ...")

    def service_unit(self, profile_name):
        return self.profile_mode(profile_name)["unit"] if profile_name in global_config else ""

    def toggle_service(self, profile_name):
        if self.profile_mode(profile_name)["active"] in ACTIVE_STATES:
            self.stop_service(profile_name)
        else:
            self.start_service(profile_name)

    def report_service_result(self, profile_name, action, ok, message, active):
        unit = self.service_unit(profile_name)
        logging.info(f"[{profile_name}] {action} {unit}: {'ok' if ok else 'failed'}, now {active}")
        if ok:
            text = f"{unit} {({'start': 'started', 'stop': 'stopped', 'restart': 'restarted'})[action]} ({active})."
            if self.tray:
                self.tray.showMessage("OneDriveGUI", text, QSystemTrayIcon.Information, 5000)
        else:
            QMessageBox.warning(
                self,
                f"Could not {action} the background service",
                f"<b>systemctl --user {action} {unit}</b> failed (state: {active}).<br><br><pre>{message}</pre>",
            )
        self.service_actions.discard(profile_name)
        self.mode_texts = {}
        self.refresh_mode_indicators()
        self.service_action_finished.emit(profile_name, action, ok, message, active)

    def resync_unit(self, profile_name):
        """The profile's automatic rebuild unit, or "" when it is not installed or not a service profile."""
        from ondemand_resync import resync_unit_for_profile, unit_installed

        if not self.service_unit(profile_name):
            return ""
        unit = resync_unit_for_profile(os.path.basename(profile_confdir(global_config[profile_name])))
        return unit if unit_installed(unit) else ""

    def run_resync(self, profile_name):
        """Start the rebuild unit and follow it until the background service is back (or stays stopped)."""
        from ondemand_resync import ResyncRunner

        unit = self.resync_unit(profile_name)
        if not unit:
            return False

        def progress_source():
            instance = self.attached_instance(profile_name)
            if instance is None:
                return ""
            props = instance.props
            counts = []
            if props.get("PendingDownloads"):
                counts.append(f"{props['PendingDownloads']} to download")
            if props.get("PendingUploads"):
                counts.append(f"{props['PendingUploads']} to upload")
            detail = props.get("StateDetail", "")
            return f"{detail} ({', '.join(counts)})" if counts else detail

        runner = ResyncRunner(unit, progress_source, self)
        self.resync_runners[profile_name] = runner
        self.service_actions.add(profile_name)
        page = self.profile_status_pages[profile_name]

        def on_progress(text):
            page.label_onedrive_status.setText(text)
            self.resync_progress.emit(profile_name, text)

        def on_finished(ok, message):
            self.resync_runners.pop(profile_name, None)
            self.service_actions.discard(profile_name)
            service = self.service_unit(profile_name)
            if ok:
                logging.info(f"[{profile_name}] Rebuild with {unit} finished")
                self.unit_states.refresh(service, force=True)
                text = f"The local index of {profile_name} has been rebuilt; {service} is starting again."
                if self.tray:
                    self.tray.showMessage("OneDriveGUI", text, QSystemTrayIcon.Information, 8000)
                page.label_onedrive_status.setText("Rebuild finished")
            else:
                logging.warning(f"[{profile_name}] Rebuild with {unit} failed: {message}")
                page.label_onedrive_status.setText("Rebuild failed: background service stopped")
                QMessageBox.warning(
                    self,
                    "Rebuilding the local index failed",
                    f"The rebuild of <b>{profile_name}</b> ({unit}) failed, so the background service <b>{service}</b> "
                    "was not started again and stays stopped. "
                    "When the cause below is fixed, start it with <i>Start background service</i>: on its first start "
                    "it rebuilds the index itself and keeps your <i>Always keep on this device</i> choices. "
                    "The rebuild can also be run from a terminal with "
                    f"<tt>onedrive-ondemand-resync {os.path.basename(profile_confdir(global_config[profile_name]))}</tt>."
                    f"<br><br><pre>{message}</pre>",
                )
            self.mode_texts = {}
            self.refresh_mode_indicators()
            self.resync_finished.emit(profile_name, ok, message)

        runner.progress.connect(on_progress)
        runner.finished.connect(on_finished)
        runner.start()
        return True

    def start_service(self, profile_name):
        """systemctl --user start <unit>; the client re-attaches over D-Bus when it is up. No confirmation."""
        unit = self.service_unit(profile_name)
        if not unit:
            return False
        self.profile_status_pages[profile_name].label_onedrive_status.setText(f"Starting {unit} ...")
        self.service_actions.add(profile_name)
        self.unit_states.start_stop(unit, "start", lambda ok, message, active: self.report_service_result(profile_name, "start", ok, message, active))
        return True

    def stop_service(self, profile_name):
        """systemctl --user stop <unit>, after a warning. Quitting the GUI never does this."""
        unit = self.service_unit(profile_name)
        if not unit:
            return False
        instance = self.attached_instance(profile_name)
        mount = (instance.props.get("SyncDir") if instance else "") or os.path.expanduser(
            global_config[profile_name]["onedrive"]["sync_dir"].strip('"')
        )
        ondemand = self.profile_mode(profile_name)["ondemand"]
        if ondemand:
            warning = (
                f"Stopping the background service unmounts <b>{mount}</b>. Files in it are unavailable until the "
                "service is started again, and apps with open files from it may lose unsaved changes. "
                "Pending uploads continue at the next start."
            )
        else:
            warning = (
                f"Stopping the background service stops syncing <b>{mount}</b>. The files stay on this computer; "
                "changes are synced when the service is started again."
            )
        answer = QMessageBox.question(
            self,
            "Stop background service ?",
            f"{warning}<br><br>Stop <b>{unit}</b>?",
            buttons=QMessageBox.Yes | QMessageBox.No,
            defaultButton=QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return False
        self.profile_status_pages[profile_name].label_onedrive_status.setText(f"Stopping {unit} ...")
        self.service_actions.add(profile_name)
        self.unit_states.start_stop(unit, "stop", lambda ok, message, active: self.report_service_result(profile_name, "stop", ok, message, active))
        return True

    def refresh_tray_menu(self):
        """(Re)build the tray menu; D-Bus entries only for attached profiles."""
        if not getattr(self, "tray_menu", None):
            return

        entries = []
        for profile_name in global_config:
            instance = self.attached_instance(profile_name)
            mode = self.profile_mode(profile_name)
            if instance is None and not mode["unit"]:
                continue
            entries.append(
                (
                    profile_name,
                    instance is not None,
                    instance is not None and instance.has_capability("pause"),
                    instance is not None and instance.props.get("State") == "paused",
                    instance is not None and bool(instance.props.get("OnDemand")),
                    self.profile_mode_text(profile_name, "\n"),
                    mode["unit"],
                    mode["active"] in ACTIVE_STATES,
                )
            )
        if entries == self.tray_menu_signature:
            return
        self.tray_menu_signature = entries

        menu = self.tray_menu
        for submenu in menu.findChildren(QMenu, options=Qt.FindDirectChildrenOnly):
            submenu.deleteLater()
        menu.clear()
        for profile_name, attached, can_pause, paused, ondemand, mode, unit, unit_running in entries:
            target = menu.addMenu(profile_name) if len(global_config) > 1 else menu
            for line in mode.splitlines():
                target.addAction(line).setEnabled(False)
            target.addSeparator()
            if unit:
                service = target.addAction("Stop background service" if unit_running else "Start background service")
                service.triggered.connect(lambda checked=False, p=profile_name: self.toggle_service(p))
            if not attached:
                continue
            target.addAction("Open folder").triggered.connect(lambda checked=False, p=profile_name: self.open_attached_sync_dir(p))
            view_online = target.addAction("View online")
            view_online.setEnabled(ondemand)
            view_online.triggered.connect(lambda checked=False, p=profile_name: self.view_attached_online(p))
            if can_pause:
                pause = target.addAction("Resume syncing" if paused else "Pause syncing")
                pause.triggered.connect(lambda checked=False, p=profile_name: self.toggle_pause(p))
            target.addAction("Sync now").triggered.connect(lambda checked=False, p=profile_name: self.sync_now(p))
            target.addAction("Status and issues").triggered.connect(lambda checked=False, p=profile_name: self.show_status_window(p))
        if entries:
            menu.addSeparator()

        show_action = menu.addAction("Show/Hide")
        show_action.triggered.connect(lambda: self.hide() if self.isVisible() else self.show())
        setting_action = menu.addAction("Settings")
        setting_action.triggered.connect(self.show_settings_window)
        quit_action = menu.addAction("Quit OneDriveGUI" if entries else "Quit")
        quit_action.triggered.connect(lambda: self.graceful_shutdown())

    def toggle_pause(self, profile_name):
        instance = self.attached_instance(profile_name)
        if instance is None:
            return
        if instance.props.get("State") == "paused":
            self.dbus.resume(instance.bus_name)
        else:
            self.dbus.pause(instance.bus_name, 0)

    def sync_now(self, profile_name):
        instance = self.attached_instance(profile_name)
        if instance is not None:
            self.dbus.sync_now(instance.bus_name)

    def open_attached_sync_dir(self, profile_name):
        instance = self.attached_instance(profile_name)
        if instance is not None and instance.props.get("SyncDir"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(instance.props["SyncDir"]))

    def view_attached_online(self, profile_name):
        instance = self.attached_instance(profile_name)
        if instance is None or not instance.props.get("SyncDir"):
            return
        sync_dir = instance.props["SyncDir"]

        def done(url, error):
            if url:
                QDesktopServices.openUrl(QUrl(url))
            elif self.tray:
                self.tray.showMessage("OneDriveGUI", f"No web link for {sync_dir}: {error}", QSystemTrayIcon.Warning, 5000)

        self.dbus.run_async(lambda: read_weburl(sync_dir), done)

    def show_status_window(self, profile_name):
        if not self.dbus:
            return
        if profile_name not in self.status_windows:
            # Parented to the main window (no taskbar entry of its own), still a separate window.
            self.status_windows[profile_name] = OnDemandStatusWindow(
                self.dbus, profile_name, self.attached.get, parent=self, mode_lookup=self.profile_mode_text
            )
            self.status_windows[profile_name].setWindowFlag(Qt.Window, True)
            self.status_windows[profile_name].setAttribute(Qt.WA_QuitOnClose, False)
            self.mode_indicators_changed.connect(self.status_windows[profile_name].refresh)
        window = self.status_windows[profile_name]
        window.show()
        window.activateWindow()
        window.raise_()

    def offer_start_service(self, profile_name):
        """Start the systemd user unit of a Files On-Demand profile instead of spawning a client."""
        unit = profile_systemd_unit(global_config[profile_name])
        if not unit:
            QMessageBox.information(
                self,
                "Files On-Demand profile",
                f"Profile <b>{profile_name}</b> is a Files On-Demand profile. OneDriveGUI does not start its own client for it; "
                "start it with <tt>onedrive --monitor --on-demand</tt> or a systemd user unit.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Start OneDrive service ?",
            f"Profile <b>{profile_name}</b> is run by the systemd user service <b>{unit}</b>, which is not running.<br><br>"
            "Start the service now?",
            buttons=QMessageBox.Yes | QMessageBox.No,
            defaultButton=QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.start_service(profile_name)

    def show_main_window(self):
        self.show()

    def hide_main_window(self):
        self.hide()


class ProfileStatusPage(QWidget, Ui_status_page):
    start_sync_signal = Signal(str)
    stop_sync_signal = Signal(str)
    quit_gui_signal = Signal()

    def __init__(self, profile_name):
        super(ProfileStatusPage, self).__init__()

        self.profile_name = profile_name

        # Set up the user interface from Designer.
        self.setupUi(self)

        # Configure Icons
        self.start_icon = QIcon(DIR_PATH + "/resources/images/play.png")
        self.stop_icon = QIcon(DIR_PATH + "/resources/images/stop.png")
        self.storage_icon = QPixmap(DIR_PATH + "/resources/images/storage.png").scaled(26, 26, Qt.KeepAspectRatio)
        self.quit_icon = QIcon(DIR_PATH + "/resources/images/quit.png")
        self.close_icon = QIcon(DIR_PATH + "/resources/images/close-filled.png")

        self.pause_icon = themed_icon(["media-playback-pause-symbolic", "media-playback-pause"], DIR_PATH + "/resources/images/stop.png")
        self.folder_icon = QIcon(DIR_PATH + "/resources/images/folder.png")
        self.profile_icon = QIcon(DIR_PATH + "/resources/images/account.png")
        self.settings_icon = QIcon(DIR_PATH + "/resources/images/gear.png")

        # Show Start/Stop buttons
        if gui_settings.get("combined_start_stop_button") == "True":
            self.pushButton_start_stop.show()
            self.pushButton_start.hide()
            self.pushButton_stop.hide()
        else:
            self.pushButton_start_stop.hide()
            self.pushButton_start.show()
            self.pushButton_stop.show()

        # Combined Start/Stop button
        self.pushButton_start_stop.setIcon(self.start_icon)
        self.pushButton_start_stop.setText("")
        self.pushButton_start_stop.clicked.connect(self.start_monitor)

        # Separate start/stop buttons
        self.pushButton_start.setIcon(self.start_icon)
        self.pushButton_start.setText("")
        self.pushButton_start.clicked.connect(self.start_monitor)

        self.pushButton_stop.setIcon(self.stop_icon)
        self.pushButton_stop.setText("")
        self.pushButton_stop.clicked.connect(self.stop_monitor)

        # Temp Quit button
        self.pushButton_quit.setIcon(self.quit_icon)
        self.pushButton_quit.setText("")
        self.pushButton_quit.clicked.connect(self.quit_gui)

        # Close Button
        if gui_settings.get("frameless_window") == "True":
            self.pushButton_close.setIcon(self.close_icon)
            self.pushButton_close.setText("")
            self.pushButton_close.clicked.connect(lambda: self.close())
        else:
            self.pushButton_close.hide()

        # Free Space Icon
        self.label_free_space_icon.setPixmap(self.storage_icon)
        self.label_free_space_icon.hide()

        # Open Sync Dir
        self.pushButton_open_dir.setText("")
        self.pushButton_open_dir.setIcon(self.folder_icon)
        self.pushButton_open_dir.clicked.connect(self.open_sync_dir)

        # Open Profiles window
        self.pushButton_profiles.setText("")
        self.pushButton_profiles.setIcon(self.profile_icon)
        self.pushButton_profiles.clicked.connect(lambda: profile_settings_window.show())
        self.pushButton_profiles.clicked.connect(lambda: profile_settings_window.start_unsaved_changes_timer())

        # Status and issues window, only for profiles whose client is attached over D-Bus
        self.pushButton_status = QPushButton(self)
        self.pushButton_status.setFlat(True)
        self.pushButton_status.setIconSize(self.pushButton_open_dir.iconSize())
        self.pushButton_status.setIcon(themed_icon(["dialog-information-symbolic", "dialog-information"], DIR_PATH + "/resources/images/warning.png"))
        self.pushButton_status.setToolTip("Transfers and issues")
        self.pushButton_status.clicked.connect(lambda: main_window_instance.show_status_window(self.profile_name))
        self.horizontalLayout.insertWidget(1, self.pushButton_status)
        self.pushButton_status.hide()

        # Open GUI Settings window
        self.pushButton_gui_settings.setText("")
        self.pushButton_gui_settings.setIcon(self.settings_icon)
        self.pushButton_gui_settings.clicked.connect(self.show_gui_settings_window)

        # Show Account Type on GUI startup (when sync is not running)
        self.label_account_type.setText(global_config[self.profile_name]["account_type"])

        # How the profile runs: Files On-Demand badge, background service or started by the GUI
        self.label_mode = QLabel(self.frame)
        self.label_mode.setStyleSheet("color: rgb(255, 255, 255);")
        self.label_mode.setAlignment(Qt.AlignCenter)
        self.label_mode.setWordWrap(True)
        self.verticalLayout_2.insertWidget(self.verticalLayout_2.indexOf(self.label_account_type) + 1, self.label_mode)

        # Start/Stop of the profile's systemd user unit (shown only for profiles run by one)
        self.pushButton_service = QPushButton(self.frame)
        self.pushButton_service.setFlat(True)
        self.pushButton_service.setStyleSheet("color: rgb(255, 255, 255); text-decoration: underline;")
        self.pushButton_service.clicked.connect(lambda: main_window_instance.toggle_service(self.profile_name))
        self.verticalLayout_2.insertWidget(self.verticalLayout_2.indexOf(self.label_mode) + 1, self.pushButton_service, 0, Qt.AlignCenter)
        self.pushButton_service.hide()

        # Allow hyperlinks in status messages
        self.label_onedrive_status.setOpenExternalLinks(True)

        # Show last known Free Space on GUI startup (when sync is not running)
        _free_space = global_config[self.profile_name]["free_space"]

        if _free_space == "0":
            self.label_free_space.setText("")
            # self.label_free_space_icon.hide()
        else:
            self.label_free_space.setText(global_config[self.profile_name]["free_space"])

    def open_sync_dir(self):
        sync_dir = global_config[self.profile_name]["onedrive"]["sync_dir"].strip('"')
        sync_dir_path = os.path.expanduser(sync_dir)
        url = QUrl.fromLocalFile(sync_dir_path)
        QDesktopServices.openUrl(url)

    def show_gui_settings_window(self):
        # self.gui_settings_window = GuiSettingsWindow()
        gui_settings_window.show()

    def stop_monitor(self):
        self.stop_sync_signal.emit(self.profile_name)

    def start_monitor(self):
        # self.start_onedrive_monitor(self.profile_name)
        self.start_sync_signal.emit(self.profile_name)

    def quit_gui(self):
        self.quit_gui_signal.emit()


class AlignDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super(AlignDelegate, self).initStyleOption(option, index)
        option.displayAlignment = Qt.AlignRight
