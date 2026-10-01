"""
Widgets for profiles whose client exposes the D-Bus status interface: tray icons per
state and the status window with transfers and issues.
"""

import logging
import os
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtCore import QMimeDatabase
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from global_config import DIR_PATH
from ondemand_dbus import classify_issues, read_weburl, tray_state
from utils.utils import humanize_file_size, format_relative_time


# Themed icon names per tray state, first existing one wins; all exist in Yaru and
# Adwaita. The bundled upstream icon is the last resort.
TRAY_ICONS = {
    "synced": (["weather-overcast-symbolic", "emblem-default"], "icons8-cloud-done-80.png"),
    "syncing": (["emblem-synchronizing-symbolic", "emblem-synchronizing", "view-refresh-symbolic"], "icons8-cloud-sync-80.png"),
    "paused": (["media-playback-pause-symbolic"], "icons8-cloud-stop-80.png"),
    "offline": (["network-offline-symbolic"], "icons8-cloud-stop-80.png"),
    "error": (["dialog-error-symbolic", "dialog-error"], "icons8-cloud-error-80.png"),
    "attention": (["dialog-warning-symbolic", "dialog-warning"], "icons8-cloud-error-80.png"),
    "stopped": (["process-stop-symbolic"], "icons8-cloud-stop-80.png"),
}

STATE_LABELS = {
    "synced": "Up to date",
    "syncing": "Syncing",
    "paused": "Paused",
    "offline": "Offline",
    "error": "Error",
    "attention": "Needs attention",
    "stopped": "Stopped",
}

ISSUE_KIND_LABELS = {
    "conflict_copy": "Kept both versions",
    "locked_online": "Locked online, retrying",
    "deferred_online_change": "Open locally, online change waits",
    "upload_failed": "Upload failed",
    "download_failed": "Download failed",
    "invalid_name": "Name not allowed",
    "too_large": "File too large",
    "permission_denied": "Permission denied",
    "quota_exceeded": "OneDrive is full",
    "other": "Problem",
}


def tray_icon_name(state):
    """Name of the themed icon used for a tray state, or None if the theme has none of them."""
    names, _fallback = TRAY_ICONS.get(state, TRAY_ICONS["synced"])
    for name in names:
        if QIcon.hasThemeIcon(name):
            return name
    return None


def tray_icon(state):
    name = tray_icon_name(state)
    if name:
        return QIcon.fromTheme(name)
    return QIcon(DIR_PATH + "/resources/images/" + TRAY_ICONS.get(state, TRAY_ICONS["synced"])[1])


def themed_icon(names, fallback):
    for name in names:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon(fallback)


def status_text(props, issues):
    """One line for status labels and tooltips: StateDetail plus the number of issues that need the user."""
    detail = props.get("StateDetail") or STATE_LABELS.get(tray_state(props), "")
    count = len(classify_issues(issues)[1])
    if count:
        detail += f" - {count} item{'s need' if count != 1 else ' needs'} attention"
    return detail


def file_type_icon(path):
    """Icon for a file by its name only; never touches the file, which may be online-only."""
    mime = QMimeDatabase().mimeTypeForFile(path, QMimeDatabase.MatchExtension)
    return QIcon.fromTheme(mime.iconName(), QIcon.fromTheme(mime.genericIconName()))


def describe_last_sync(unix_time):
    if not unix_time:
        return "never"
    return format_relative_time(datetime.fromtimestamp(unix_time))


class OnDemandStatusWindow(QWidget):
    """Transfers and issues of one profile attached over D-Bus."""

    def __init__(self, dbus, profile_name, bus_name_lookup, parent=None, mode_lookup=None):
        super().__init__(parent)
        self.mode_lookup = mode_lookup
        self.dbus = dbus
        self.profile_name = profile_name
        self.bus_name_lookup = bus_name_lookup

        self.setWindowTitle(f"OneDrive status - {profile_name}")
        self.setWindowIcon(QIcon(DIR_PATH + "/resources/images/icons8-cloud-80.png"))
        self.resize(640, 460)

        self.label_state = QLabel()
        self.label_state.setTextFormat(Qt.PlainText)
        font = self.label_state.font()
        font.setBold(True)
        self.label_state.setFont(font)
        self.label_details = QLabel()
        self.label_details.setTextFormat(Qt.PlainText)
        self.label_message = QLabel()
        self.label_message.setTextFormat(Qt.PlainText)
        self.label_message.setWordWrap(True)

        self.button_sync_now = QPushButton("Sync now")
        self.button_sync_now.clicked.connect(self.sync_now)
        self.button_pause = QPushButton("Pause")
        self.button_pause.clicked.connect(self.toggle_pause)
        self.button_open_sync_dir = QPushButton("Open folder")
        self.button_open_sync_dir.clicked.connect(self.open_sync_dir)

        controls = QHBoxLayout()
        controls.addWidget(self.button_sync_now)
        controls.addWidget(self.button_pause)
        controls.addStretch()
        controls.addWidget(self.button_open_sync_dir)

        # Transfers
        self.tree_transfers = QTreeWidget()
        self.tree_transfers.setHeaderLabels(["File", "Direction", "Progress", "Size"])
        self.tree_transfers.setRootIsDecorated(False)
        self.tree_transfers.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree_transfers.header().setSectionResizeMode(0, QHeaderView.Stretch)

        # Issues
        self.tree_issues = QTreeWidget()
        self.tree_issues.setHeaderLabels(["File", "Problem", "When"])
        self.tree_issues.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_issues.itemSelectionChanged.connect(self.update_issue_buttons)
        self.tree_issues.itemDoubleClicked.connect(lambda item, _column: self.open_issue_folder())
        self.group_attention = QTreeWidgetItem(self.tree_issues, ["Needs attention"])
        self.group_handled = QTreeWidgetItem(self.tree_issues, ["Handled automatically"])
        for group in (self.group_attention, self.group_handled):
            group.setFlags(Qt.ItemIsEnabled)
            group.setFirstColumnSpanned(True)
            group.setExpanded(True)

        self.button_open_file = QPushButton("Open file")
        self.button_open_file.clicked.connect(self.open_issue_file)
        self.button_open_folder = QPushButton("Open folder")
        self.button_open_folder.clicked.connect(self.open_issue_folder)
        self.button_view_online = QPushButton("View online")
        self.button_view_online.clicked.connect(self.view_issue_online)
        self.button_dismiss = QPushButton("Dismiss")
        self.button_dismiss.clicked.connect(self.dismiss_issue)

        issue_buttons = QHBoxLayout()
        for button in (self.button_open_file, self.button_open_folder, self.button_view_online):
            issue_buttons.addWidget(button)
        issue_buttons.addStretch()
        issue_buttons.addWidget(self.button_dismiss)

        issues_page = QWidget()
        issues_layout = QVBoxLayout(issues_page)
        issues_layout.addWidget(self.tree_issues)
        issues_layout.addLayout(issue_buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.tree_transfers, "Transfers")
        self.tabs.addTab(issues_page, "Issues")

        layout = QVBoxLayout(self)
        layout.addWidget(self.label_state)
        layout.addWidget(self.label_details)
        layout.addLayout(controls)
        layout.addWidget(self.tabs)
        layout.addWidget(self.label_message)

        dbus.properties_changed.connect(self._on_instance_event)
        dbus.transfers_changed.connect(self._on_instance_event)
        dbus.issues_changed.connect(self._on_instance_event)
        dbus.instance_added.connect(self._on_instance_event)
        dbus.instance_removed.connect(self._on_instance_event)

        self.refresh()

    # State

    def instance(self):
        bus_name = self.bus_name_lookup(self.profile_name)
        return self.dbus.instances.get(bus_name) if bus_name else None

    def _on_instance_event(self, bus_name):
        current = self.bus_name_lookup(self.profile_name)
        if self.isVisible() and (current is None or bus_name == current):
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        instance = self.instance()
        if instance is None:
            self.label_state.setText("The client for this profile is not running.")
            self.label_details.setText("")
            for button in (self.button_sync_now, self.button_pause, self.button_open_sync_dir):
                button.setEnabled(False)
            self.populate_transfers([])
            self.populate_issues([])
            return

        props = instance.props
        self.label_state.setText(status_text(props, instance.issues))

        details = [props.get("Account") or self.profile_name, f"last sync {describe_last_sync(props.get('LastSyncTime', 0))}"]
        if props.get("QuotaTotal"):
            details.append(f"{humanize_file_size(props['QuotaUsed'])} of {humanize_file_size(props['QuotaTotal'])} used")
        if props.get("OnDemand") and not self.mode_lookup:
            details.append("Files On-Demand")
        self.label_details.setText(" - ".join(details))
        if self.mode_lookup:
            self.label_details.setText(self.label_details.text() + "\n" + self.mode_lookup(self.profile_name))

        self.button_sync_now.setEnabled(True)
        self.button_open_sync_dir.setEnabled(bool(props.get("SyncDir")))
        self.button_pause.setVisible(instance.has_capability("pause"))
        self.button_pause.setText("Resume" if props.get("State") == "paused" else "Pause")

        self.tabs.setTabVisible(0, instance.has_capability("transfers"))
        self.tabs.setTabVisible(1, instance.has_capability("issues"))
        self.populate_transfers(instance.transfers)
        self.populate_issues(instance.issues)

    def populate_transfers(self, transfers):
        self.tree_transfers.clear()
        for path, direction, state, done, total in transfers:
            item = QTreeWidgetItem(self.tree_transfers, [path, direction, "", humanize_file_size(total) if total else ""])
            item.setToolTip(0, path)
            if state == "queued":
                item.setText(2, "Queued")
            else:
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(int(done * 100 / total) if total else 0)
                self.tree_transfers.setItemWidget(item, 2, bar)
        self.tabs.setTabText(0, f"Transfers ({len(transfers)})")

    def populate_issues(self, issues):
        selected = self.selected_issue()
        selected_id = selected[0] if selected else None

        handled, attention = classify_issues(issues)
        for group, entries, title in (
            (self.group_attention, attention, "Needs attention"),
            (self.group_handled, handled, "Handled automatically"),
        ):
            group.takeChildren()
            group.setText(0, f"{title} ({len(entries)})")
            for issue in entries:
                issue_id, path, kind, _severity, message, unix_time = issue
                item = QTreeWidgetItem(group, [path, ISSUE_KIND_LABELS.get(kind, kind), describe_last_sync(unix_time)])
                item.setToolTip(0, path)
                item.setToolTip(1, message)
                item.setData(0, Qt.UserRole, issue)
                if issue_id == selected_id:
                    item.setSelected(True)
        self.tabs.setTabText(1, f"Issues ({len(attention)})" if attention else "Issues")
        self.update_issue_buttons()

    # Issue actions

    def selected_issue(self):
        items = self.tree_issues.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def issue_path(self, issue):
        instance = self.instance()
        if instance is None or not issue:
            return None
        return os.path.join(instance.props.get("SyncDir", ""), issue[1])

    def update_issue_buttons(self):
        issue = self.selected_issue()
        instance = self.instance()
        has_issue = issue is not None and instance is not None
        for button in (self.button_open_file, self.button_open_folder, self.button_dismiss):
            button.setEnabled(has_issue)
        # The web URL comes from the on-demand mount (xattr user.onedrive.weburl).
        self.button_view_online.setEnabled(has_issue and bool(instance.props.get("OnDemand")))

    def open_issue_file(self):
        path = self.issue_path(self.selected_issue())
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_issue_folder(self):
        path = self.issue_path(self.selected_issue())
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def view_issue_online(self):
        path = self.issue_path(self.selected_issue())
        if path:
            self.view_online(path)

    def view_online(self, path):
        def done(url, error):
            if error or not url:
                self.label_message.setText(f"No web link for {os.path.basename(path)}: {error}")
                logging.warning(f"[{self.profile_name}] weburl for {path} failed: {error}")
            else:
                self.label_message.setText("")
                QDesktopServices.openUrl(QUrl(url))

        self.dbus.run_async(lambda: read_weburl(path), done)

    def dismiss_issue(self):
        issue = self.selected_issue()
        instance = self.instance()
        if issue and instance:
            self.dbus.dismiss_issue(instance.bus_name, issue[0], callback=self._report_error("Dismiss"))

    # Instance actions

    def sync_now(self):
        instance = self.instance()
        if instance:
            self.dbus.sync_now(instance.bus_name, callback=self._report_error("Sync now"))

    def toggle_pause(self):
        instance = self.instance()
        if instance is None:
            return
        if instance.props.get("State") == "paused":
            self.dbus.resume(instance.bus_name, callback=self._report_error("Resume"))
        else:
            self.dbus.pause(instance.bus_name, 0, callback=self._report_error("Pause"))

    def open_sync_dir(self):
        instance = self.instance()
        if instance and instance.props.get("SyncDir"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(instance.props["SyncDir"]))

    def _report_error(self, action):
        def done(_result, error):
            self.label_message.setText(f"{action} failed: {error}" if error else "")

        return done
