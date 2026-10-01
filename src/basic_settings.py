"""
"Basic" profile settings: the few things most people change, like the Windows client's settings.
Everything else stays in upstream's editor, which is shown under "Advanced".

The page edits the same pending profile configuration as the Advanced editor (through its
widgets), so Save and Discard of the settings window cover both.
"""

import logging
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ondemand_mode import ONDEMAND_BADGE, free_up_space, mode_text, profile_mode


def main_window():
    import main_window as module

    return getattr(module, "main_window_instance", None)


class BasicSettingsPage(QWidget):
    def __init__(self, settings_page, parent=None):
        super().__init__(parent)
        self.settings_page = settings_page  # the upstream ProfileSettingsPage (Advanced)
        self._syncing_text = False
        self.freeing = False
        self.free_space_result = ""
        self.service_busy = False
        self.service_result = ""

        self.label_account = QLabel()
        self.label_account.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label_mode = QLabel()
        self.label_mode.setWordWrap(True)

        self.lineEdit_folder = QLineEdit()
        self.lineEdit_folder.textEdited.connect(self.folder_edited)
        self.pushButton_folder_browse = QPushButton("Change...")
        self.pushButton_folder_browse.clicked.connect(self.browse_folder)
        self.pushButton_folder_open = QPushButton("Open")
        self.pushButton_folder_open.clicked.connect(self.open_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.lineEdit_folder)
        folder_row.addWidget(self.pushButton_folder_browse)
        folder_row.addWidget(self.pushButton_folder_open)
        self.label_folder_note = QLabel()
        self.label_folder_note.setWordWrap(True)

        self.label_ondemand = QLabel()
        self.label_ondemand.setWordWrap(True)

        self.checkBox_start_at_login = QCheckBox()
        self.checkBox_start_at_login.clicked.connect(self.start_at_login_clicked)
        self.label_start_at_login = QLabel()
        self.label_start_at_login.setWordWrap(True)

        self.pushButton_pause = QPushButton()
        self.pushButton_pause.clicked.connect(self.toggle_pause)

        self.pushButton_service_start = QPushButton("Start background service")
        self.pushButton_service_start.clicked.connect(lambda: self.service_action("start"))
        self.pushButton_service_stop = QPushButton("Stop background service")
        self.pushButton_service_stop.clicked.connect(lambda: self.service_action("stop"))
        service_row = QHBoxLayout()
        service_row.addWidget(self.pushButton_service_start)
        service_row.addWidget(self.pushButton_service_stop)
        self.label_service = QLabel()
        self.label_service.setWordWrap(True)
        self.label_service.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.textEdit_folders = QPlainTextEdit()
        self.textEdit_folders.setPlaceholderText("Empty: all of OneDrive. One folder per line, e.g. /Documents")
        self.textEdit_folders.setMaximumHeight(140)
        self.textEdit_folders.textChanged.connect(self.folders_edited)
        settings_page.textEdit_sync_list.textChanged.connect(self.folders_changed_in_advanced)
        self.label_folders_note = QLabel()
        self.label_folders_note.setWordWrap(True)

        self.pushButton_free_space = QPushButton("Free up space for the whole drive")
        self.pushButton_free_space.clicked.connect(self.free_up_space_clicked)
        self.label_free_space = QLabel()
        self.label_free_space.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Account", self.label_account)
        form.addRow("Runs", self.label_mode)
        form.addRow("Folder location", folder_row)
        form.addRow("", self.label_folder_note)
        form.addRow("Files On-Demand", self.label_ondemand)
        form.addRow("Start at login", self.checkBox_start_at_login)
        form.addRow("", self.label_start_at_login)
        self.label_service_row = QLabel("Background service")
        form.addRow(self.label_service_row, service_row)
        form.addRow("", self.label_service)
        form.addRow("Syncing", self.pushButton_pause)
        form.addRow("Folders to show", self.textEdit_folders)
        form.addRow("", self.label_folders_note)
        form.addRow("Space", self.pushButton_free_space)
        form.addRow("", self.label_free_space)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch()

        self.refresh()

    # Facts

    @property
    def profile(self):
        return self.settings_page.profile

    def profile_config(self):
        import options

        return options.global_config.get(self.profile, self.settings_page.temp_profile_config)

    def instance(self):
        window = main_window()
        return window.attached_instance(self.profile) if window else None

    def mode(self):
        window = main_window()
        from workers import workers

        return profile_mode(
            self.profile_config(),
            self.instance(),
            self.profile in workers,
            window.unit_states if window else None,
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        window = main_window()
        if window and not getattr(self, "_connected", False):
            self._connected = True
            window.mode_indicators_changed.connect(self.refresh)
            window.service_action_finished.connect(self.service_action_finished)

    def refresh(self):
        config = self.settings_page.temp_profile_config
        instance = self.instance()
        mode = self.mode()

        account = (instance.props.get("Account") if instance else "") or self.profile
        account_type = (instance.props.get("AccountType") if instance else "") or config.get("account_type", "")
        self.label_account.setText(f"{account} ({account_type.capitalize()})" if account_type and account_type != "unknown" else account)
        self.label_mode.setText(mode_text(mode, "\n"))

        if mode["ondemand"]:
            if self.settings_page.is_resync_key("sync_dir"):
                self.label_folder_note.setText("Changing the folder makes the client rebuild its local index; downloaded files are kept (you are asked before saving).")
            else:
                self.label_folder_note.setText("Changing the folder moves where OneDrive appears when the service restarts (you are asked before saving).")
            self.label_folders_note.setText("Changing the folder selection makes the client rebuild its local index; downloaded files are kept (you are asked before saving).")
        else:
            self.label_folder_note.setText("Changing the folder makes the client resynchronise everything (you are asked before saving).")
            self.label_folders_note.setText("Changing the folder selection makes the client resynchronise (you are asked before saving).")

        if not self.lineEdit_folder.hasFocus():
            self.lineEdit_folder.setText(self.settings_page.lineEdit_sync_dir.text())
        if mode["ondemand"]:
            self.label_ondemand.setText(f"On: files appear at once and are downloaded when opened ({ONDEMAND_BADGE}).")
        else:
            self.label_ondemand.setText(
                "Off: every file is downloaded. Turning Files On-Demand on needs a new Files On-Demand profile "
                "(Profiles > Create/Import) or a resync of this one."
            )

        if mode["unit"]:
            enabled = mode["enabled"]
            self.checkBox_start_at_login.setText(f"Start {mode['unit']} at login")
            self.checkBox_start_at_login.setChecked(enabled in ("enabled", "enabled-runtime", "linked"))
            self.checkBox_start_at_login.setEnabled(enabled not in ("unknown", "static", "masked"))
            self.label_start_at_login.setText(f"systemd user unit, currently {enabled}, {mode['active']}. Applied immediately.")
        else:
            self.checkBox_start_at_login.setText("Start syncing when OneDriveGUI starts")
            self.checkBox_start_at_login.setChecked(self.settings_page.checkBox_auto_sync.isChecked())
            self.checkBox_start_at_login.setEnabled(True)
            self.label_start_at_login.setText("Saved with the other settings.")

        has_unit = bool(mode["unit"])
        running = mode["active"] in ("active", "activating", "reloading")
        for widget in (self.label_service_row, self.pushButton_service_start, self.pushButton_service_stop, self.label_service):
            widget.setVisible(has_unit)
        if has_unit:
            self.pushButton_service_start.setEnabled(not running and not self.service_busy)
            self.pushButton_service_stop.setEnabled(running and not self.service_busy)
            if not self.service_result:
                self.label_service.setText(f"{mode['unit']}: {mode['active']}")

        can_pause = instance is not None and instance.has_capability("pause")
        self.pushButton_pause.setVisible(can_pause)
        self.pushButton_pause.setText("Resume syncing" if can_pause and instance.props.get("State") == "paused" else "Pause syncing")

        if self.textEdit_folders.toPlainText() != self.settings_page.textEdit_sync_list.toPlainText():
            self.folders_changed_in_advanced()

        sync_dir = instance.props.get("SyncDir") if instance else ""
        can_free = bool(mode["ondemand"] and instance is not None and sync_dir)
        self.pushButton_free_space.setVisible(bool(mode["ondemand"]))
        self.pushButton_free_space.setEnabled(can_free and not self.freeing)
        if not self.free_space_result:
            if can_free:
                self.label_free_space.setText("Removes the local copies; files stay online and download again when opened.")
            else:
                self.label_free_space.setText("Available while the client for this profile is running.")
        self.label_free_space.setVisible(bool(mode["ondemand"]))

    # Folder location (the Advanced editor's sync_dir field holds the pending value)

    def folder_edited(self, text):
        self.settings_page.lineEdit_sync_dir.setText(text)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, dir=os.path.expanduser(self.lineEdit_folder.text() or "~"))
        if folder:
            self.lineEdit_folder.setText(folder)
            self.folder_edited(folder)

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.expanduser(self.lineEdit_folder.text())))

    # Start at login

    def start_at_login_clicked(self, checked):
        mode = self.mode()
        if not mode["unit"]:
            self.settings_page.checkBox_auto_sync.setChecked(checked)
            return
        window = main_window()
        if window is None:
            return
        self.checkBox_start_at_login.setEnabled(False)

        def done(ok, output):
            self.label_start_at_login.setText("" if ok else f"systemctl failed: {output}")
            self.checkBox_start_at_login.setEnabled(True)
            if not ok:
                self.checkBox_start_at_login.setChecked(not checked)

        window.unit_states.set_enabled(mode["unit"], checked, done)

    # Background service (systemd user unit)

    def service_action(self, action):
        window = main_window()
        if window is None:
            return
        # False when there is no unit or the stop was not confirmed
        started = (window.start_service if action == "start" else window.stop_service)(self.profile)
        if started:
            self.service_busy = True
            self.service_result = ""
        self.refresh()

    def service_action_finished(self, profile, action, ok, message, active):
        if profile != self.profile:
            return
        self.service_busy = False
        unit = self.mode()["unit"]
        if ok:
            self.service_result = f"{unit} {({'start': 'started', 'stop': 'stopped', 'restart': 'restarted'})[action]}: {active}"
        else:
            self.service_result = f"{action.capitalize()} failed ({active}):\n{message}"
        self.label_service.setText(self.service_result)
        self.refresh()

    # Pause

    def toggle_pause(self):
        window = main_window()
        if window:
            window.toggle_pause(self.profile)

    # Folder selection: mirrors the Advanced editor's sync_list text (saved by its guarded writer)

    def folders_edited(self):
        if self._syncing_text:
            return
        self._syncing_text = True
        self.settings_page.textEdit_sync_list.setPlainText(self.textEdit_folders.toPlainText())
        self._syncing_text = False

    def folders_changed_in_advanced(self):
        if self._syncing_text:
            return
        self._syncing_text = True
        self.textEdit_folders.setPlainText(self.settings_page.textEdit_sync_list.toPlainText())
        self._syncing_text = False

    # Free up space

    def free_up_space_clicked(self):
        instance = self.instance()
        window = main_window()
        if instance is None or window is None:
            return
        mount = instance.props.get("SyncDir", "")
        answer = QMessageBox.question(
            self,
            "Free up space ?",
            f"Remove the local copies of all files in <b>{mount}</b>?<br><br>"
            "Files stay in OneDrive and are downloaded again when you open them. "
            "Folders set to <i>Always keep on this device</i> are unpinned first. "
            "Files with changes that are not uploaded yet, or that are open, are kept.",
            buttons=QMessageBox.Yes | QMessageBox.No,
            defaultButton=QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.freeing = True
        self.pushButton_free_space.setEnabled(False)
        self.free_space_result = "Freeing up space ..."
        self.label_free_space.setText(self.free_space_result)

        def done(result, error):
            self.freeing = False
            self.pushButton_free_space.setEnabled(True)
            if error:
                self.free_space_result = f"Could not free up space: {error}"
                self.label_free_space.setText(self.free_space_result)
                return
            accepted, refused = result
            text = f"Requested for {len(accepted)} item(s); the client frees them in the background."
            if refused:
                text += " Not freed: " + ", ".join(f"{name} ({reason})" for name, reason in sorted(refused.items()))
            self.free_space_result = text
            self.label_free_space.setText(text)
            logging.info(f"[{self.profile}] Free up space: {accepted} refused {refused}")

        window.dbus.run_async(lambda: free_up_space(mount), done)
