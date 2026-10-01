"""
Tests for the D-Bus integration. Run through tests/run_tests.sh, which provides a private
session bus (dbus-run-session), a throw-away HOME and QT_QPA_PLATFORM=offscreen.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(os.path.dirname(TESTS_DIR), "src")
MOCK = os.path.join(TESTS_DIR, "mock_onedrive_dbus.py")
sys.path.insert(0, SRC_DIR)

REAL_HOME_BUS = f"unix:path=/run/user/{os.getuid()}/bus"
if os.environ.get("DBUS_SESSION_BUS_ADDRESS", REAL_HOME_BUS) == REAL_HOME_BUS or not os.environ.get("ONEDRIVEGUI_TEST_HOME"):
    raise SystemExit("Run these tests through tests/run_tests.sh (private bus and HOME).")

HOME = os.environ["HOME"]
CONFDIR_A = os.path.join(HOME, ".config", "profile-a")
CONFDIR_B = os.path.join(HOME, ".config", "profile-b")
MOUNT_A = os.path.join(HOME, "OneDrive-a")

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

app = QApplication.instance() or QApplication(sys.argv)

import ondemand_dbus as od
import ondemand_profile


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def start_mock(confdir, *extra):
    process = subprocess.Popen(["/usr/bin/python3", MOCK, "--confdir", confdir, "--syncdir", MOUNT_A, *extra], stdout=subprocess.PIPE, text=True)
    assert "owning" in process.stdout.readline()
    return process


def stop_mock(process):
    if process.poll() is None:
        process.kill()
    process.wait()
    process.stdout.close()


def control(confdir, method, *args):
    """Call the mock's test-only control interface with gdbus."""
    return subprocess.run(
        ["gdbus", "call", "--session", "--dest", od.bus_name_for_confdir(confdir), "--object-path", od.OBJECT_PATH, "--method", f"{od.INTERFACE}.MockControl.{method}", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class PureLogicTests(unittest.TestCase):
    def test_bus_name_from_confdir(self):
        expected = "io.github.abraunegg.OneDrive.i" + hashlib.sha256(b"/home/u/.config/onedrive").hexdigest()[:16]
        self.assertEqual(od.bus_name_for_confdir("/home/u/.config/onedrive"), expected)
        self.assertEqual(od.bus_name_for_confdir("/home/u/.config/onedrive/x/.."), expected)
        self.assertEqual(len(expected) - len(od.BUS_NAME_PREFIX), 16)

    def test_match_profiles_by_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.mkdir(real)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            instances = {"bus.a": {"ConfigDir": real}, "bus.other": {"ConfigDir": "/nowhere"}, "bus.empty": {}}
            confdirs = {"a": link + "/", "b": os.path.join(tmp, "b")}
            self.assertEqual(od.match_profiles(confdirs, instances), {"a": "bus.a"})

    def test_profile_confdir(self):
        self.assertEqual(od.profile_confdir({"config_file": '"~/.config/x/config"'}), os.path.expanduser("~/.config/x"))

    def test_start_decision(self):
        plain = {"config_file": "/c/config"}
        ondemand = {"config_file": "/c/config", "ondemand": "True", "systemd_unit": "onedrive-ondemand@c.service"}
        self.assertEqual(od.start_decision(plain, attached=False, gui_owns_worker=False), "spawn")
        self.assertEqual(od.start_decision(plain, attached=True, gui_owns_worker=False), "attach")
        self.assertEqual(od.start_decision(plain, attached=True, gui_owns_worker=True), "running")
        self.assertEqual(od.start_decision(ondemand, attached=False, gui_owns_worker=False), "service")
        self.assertEqual(od.start_decision(ondemand, attached=True, gui_owns_worker=False), "attach")
        self.assertEqual(od.start_decision({**ondemand, "ondemand": "False"}, attached=False, gui_owns_worker=False), "spawn")

    def test_imported_ondemand_profile_is_never_spawned(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(HOME, ".config")) as confdir:
            imported = {"config_file": os.path.join(confdir, "config")}
            self.assertEqual(od.start_decision(imported, attached=False, gui_owns_worker=False), "spawn")
            os.mkdir(os.path.join(confdir, "ondemand"))
            self.assertEqual(od.start_decision(imported, attached=False, gui_owns_worker=False), "service")
            self.assertEqual(od.profile_systemd_unit(imported), f"onedrive-ondemand@{os.path.basename(confdir)}.service")
        self.assertEqual(od.profile_systemd_unit({"config_file": "/elsewhere/p/config"}), "")

    def test_classify_issues(self):
        issues = [
            ("1", "a", "conflict_copy", "info", "m", 10),
            ("2", "b", "invalid_name", "attention", "m", 30),
            ("3", "c", "locked_online", "info", "m", 20),
            ("4", "d", "upload_failed", "attention", "m", 5),
            ("5", "e", "deferred_online_change", "", "m", 1),  # unknown severity: by kind
            ("6", "f", "something_new", "", "m", 2),
        ]
        handled, attention = od.classify_issues(issues)
        self.assertEqual([i[0] for i in handled], ["3", "1", "5"])
        self.assertEqual([i[0] for i in attention], ["2", "4", "6"])

    def test_tray_state(self):
        attention = [("1", "a", "invalid_name", "attention", "m", 1)]
        info = [("1", "a", "conflict_copy", "info", "m", 1)]
        cases = {
            "idle": "synced",
            "syncing": "syncing",
            "starting": "syncing",
            "paused": "paused",
            "offline": "offline",
            "error": "error",
            "stopping": "stopped",
        }
        for state, expected in cases.items():
            self.assertEqual(od.tray_state({"State": state}), expected, state)
            self.assertEqual(od.tray_state({"State": state}, info), expected, state)
        self.assertEqual(od.tray_state({"State": "idle"}, attention), "attention")
        self.assertEqual(od.tray_state({"State": "syncing"}, attention), "attention")
        self.assertEqual(od.tray_state({"State": "paused"}, attention), "paused")
        self.assertEqual(od.tray_state({"State": "error"}, attention), "error")

    def test_aggregate_tray_state(self):
        self.assertEqual(od.aggregate_tray_state({"synced", "syncing"}), "syncing")
        self.assertEqual(od.aggregate_tray_state({"synced", "attention", "paused"}), "attention")
        self.assertEqual(od.aggregate_tray_state({"offline", "error"}), "error")
        self.assertEqual(od.aggregate_tray_state(set()), "synced")

    def test_systemd_unit(self):
        self.assertEqual(od.systemd_unit_for_profile("onedrive-ondemand"), "onedrive-ondemand.service")
        self.assertEqual(od.systemd_unit_for_profile("work"), "onedrive-ondemand@work.service")

    def test_setup_command_never_forces_or_enables(self):
        args = ondemand_profile.setup_command("work", "~/OneDrive-Work", "contoso.onmicrosoft.com", "app-id")
        self.assertNotIn("--force", args)
        self.assertNotIn("--enable-service", args)
        self.assertIn("--no-enable-service", args)
        self.assertIn("--no-auth", args)
        self.assertEqual(args[args.index("--profile") + 1], "work")
        self.assertIn("--business", args)
        self.assertNotIn("--business", ondemand_profile.setup_command("p", "~/m"))

    def test_validate_and_register_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            full = os.path.join(tmp, "full")
            os.mkdir(full)
            open(os.path.join(full, "f"), "w").close()
            self.assertEqual(ondemand_profile.validate_new_profile("ok", os.path.join(tmp, "new"), [], []), "")
            self.assertIn("empty", ondemand_profile.validate_new_profile("ok", full, [], []))
            self.assertIn("already", ondemand_profile.validate_new_profile("ok", "~/x", ["ok"], []))
            self.assertIn("letters", ondemand_profile.validate_new_profile("bad name", "~/x", [], []))
            self.assertIn("another profile", ondemand_profile.validate_new_profile("ok", tmp + "/x", [], [tmp + "/x/"]))

            profiles = os.path.join(tmp, "gui", "profiles")
            entry = ondemand_profile.add_gui_profile(profiles, "work")
            self.assertEqual(entry["systemd_unit"], "onedrive-ondemand@work.service")
            with open(profiles) as f:
                text = f.read()
            self.assertIn("[work]", text)
            self.assertIn("ondemand = True", text)

    def test_weburl_xattr_read(self):
        with tempfile.NamedTemporaryFile(dir=HOME) as f:
            try:
                os.setxattr(f.name, "user.onedrive.weburl", b"https://example.invalid/item\x00")
            except OSError:
                self.skipTest("user xattrs not supported on this file system")
            self.assertEqual(od.read_weburl(f.name), "https://example.invalid/item")


class IconTests(unittest.TestCase):
    def test_states_have_themed_icons_in_yaru(self):
        from ondemand_ui import TRAY_ICONS, tray_icon_name

        if not os.path.isdir("/usr/share/icons/Yaru"):
            self.skipTest("Yaru not installed")
        # The offscreen platform has no desktop theme integration, so point Qt at the system
        # icon directories the way the desktop platform themes do.
        previous, previous_paths = QIcon.themeName(), QIcon.themeSearchPaths()
        QIcon.setThemeSearchPaths(previous_paths + ["/usr/share/icons"])
        QIcon.setThemeName("Yaru")
        try:
            for state in TRAY_ICONS:
                self.assertIsNotNone(tray_icon_name(state), state)
                self.assertFalse(QIcon.fromTheme(tray_icon_name(state)).pixmap(22, 22).isNull(), state)
        finally:
            QIcon.setThemeName(previous)
            QIcon.setThemeSearchPaths(previous_paths)

    def test_bundled_fallback(self):
        from ondemand_ui import TRAY_ICONS, tray_icon

        previous = QIcon.themeName()
        QIcon.setThemeName("no-such-theme")
        try:
            for state, (_names, fallback) in TRAY_ICONS.items():
                self.assertTrue(os.path.exists(os.path.join(SRC_DIR, "resources", "images", fallback)), fallback)
                self.assertFalse(tray_icon(state).isNull(), state)
        finally:
            QIcon.setThemeName(previous)


class DBusClientTests(unittest.TestCase):
    """OneDriveDBus against the mock service on the private bus."""

    def setUp(self):
        self.mock = start_mock(CONFDIR_A, "--state", "idle")
        self.dbus = od.OneDriveDBus()
        self.assertTrue(self.dbus.start())

    def tearDown(self):
        self.dbus.stop()
        stop_mock(self.mock)

    def instance(self):
        return self.dbus.instances.get(od.bus_name_for_confdir(CONFDIR_A))

    def test_discovery_and_properties(self):
        instance = self.instance()
        self.assertIsNotNone(instance)
        self.assertEqual(instance.props["ConfigDir"], CONFDIR_A)
        self.assertEqual(instance.props["State"], "idle")
        self.assertTrue(instance.has_capability("pause"))

    def test_property_and_issue_signals(self):
        changed = []
        self.dbus.properties_changed.connect(changed.append)
        control(CONFDIR_A, "SetProp", "State", "<'offline'>")
        self.assertTrue(wait_until(lambda: self.instance().props["State"] == "offline"))
        self.assertTrue(changed)

        control(CONFDIR_A, "AddIssue", "Docs/a:b.txt", "invalid_name", "attention", "bad name")
        self.assertTrue(wait_until(lambda: len(self.instance().issues) == 1))
        self.assertEqual(od.tray_state({"State": "idle"}, self.instance().issues), "attention")

        control(CONFDIR_A, "SetTransfers", "[('Docs/x.bin', 'upload', 'active', uint64 5, uint64 10)]")
        self.assertTrue(wait_until(lambda: len(self.instance().transfers) == 1))
        self.assertEqual(self.instance().transfers[0], ("Docs/x.bin", "upload", "active", 5, 10))

    def test_methods(self):
        results = []
        bus_name = self.instance().bus_name
        self.dbus.pause(bus_name, 15, callback=lambda r, e: results.append(e))
        self.assertTrue(wait_until(lambda: self.instance().props["State"] == "paused"))
        self.dbus.resume(bus_name, callback=lambda r, e: results.append(e))
        self.assertTrue(wait_until(lambda: self.instance().props["State"] == "idle"))
        issue_id = control(CONFDIR_A, "AddIssue", "a", "conflict_copy", "info", "m").strip("()',")
        self.assertTrue(wait_until(lambda: len(self.instance().issues) == 1))
        self.dbus.dismiss_issue(bus_name, issue_id, callback=lambda r, e: results.append(e))
        self.assertTrue(wait_until(lambda: len(self.instance().issues) == 0))
        self.dbus.sync_now(bus_name, callback=lambda r, e: results.append(e))
        self.assertTrue(wait_until(lambda: len(results) == 4))
        self.assertEqual(results, [None] * 4)
        log = control(CONFDIR_A, "CallLog")
        for call in ("Pause(15,)", "Resume", "DismissIssue", "SyncNow"):
            self.assertIn(call, log)

    def test_instance_appears_and_leaves(self):
        removed, added = [], []
        self.dbus.instance_removed.connect(removed.append)
        self.dbus.instance_added.connect(added.append)
        bus_name = self.instance().bus_name
        stop_mock(self.mock)
        self.assertTrue(wait_until(lambda: removed == [bus_name]))
        self.assertNotIn(bus_name, self.dbus.instances)
        self.mock = start_mock(CONFDIR_A)
        self.assertTrue(wait_until(lambda: added == [bus_name]))

    def test_pause_without_capability_reports_error(self):
        other = start_mock(CONFDIR_B, "--capabilities", "issues,transfers")
        try:
            bus_name = od.bus_name_for_confdir(CONFDIR_B)
            self.assertTrue(wait_until(lambda: bus_name in self.dbus.instances))
            self.assertFalse(self.dbus.instances[bus_name].has_capability("pause"))
            errors = []
            self.dbus.pause(bus_name, 0, callback=lambda r, e: errors.append(e))
            self.assertTrue(wait_until(lambda: errors))
            self.assertIsNotNone(errors[0])
        finally:
            stop_mock(other)


class MainWindowTests(unittest.TestCase):
    """The real MainWindow with two GUI profiles: A has a client on the bus, B does not."""

    @classmethod
    def setUpClass(cls):
        cls.mock = start_mock(CONFDIR_A, "--state", "syncing", "--detail", "Uploading 3 files")
        import main_window
        from workers import workers

        cls.main_window_module = main_window
        cls.workers = workers
        cls.window = main_window.MainWindow()
        # No system tray on the offscreen platform; give the window a tray object and menu to drive.
        cls.window.tray = QSystemTrayIcon()
        cls.window.tray_menu = QMenu()
        cls.window.tray_menu_signature = None
        cls.window.refresh_tray_menu()

    @classmethod
    def tearDownClass(cls):
        cls.window.dbus.stop()
        stop_mock(cls.mock)

    def test_attaches_to_running_client(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        self.assertEqual(self.window.attached, {"profile-a": od.bus_name_for_confdir(CONFDIR_A)})
        page = self.window.profile_status_pages["profile-a"]
        self.assertIn("Uploading 3 files", page.label_onedrive_status.text())
        self.assertFalse(page.pushButton_status.isHidden())
        self.assertTrue(self.window.profile_status_pages["profile-b"].pushButton_status.isHidden())

    def test_start_does_not_spawn_for_attached_profile(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        self.window.start_onedrive_monitor("profile-a")
        self.window.autostart_monitor()
        self.assertNotIn("profile-a", self.workers)
        self.assertEqual(self.workers, {})

    def test_start_stop_button_pauses_attached_client(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        self.window.onedrive_process_status()
        page = self.window.profile_status_pages["profile-a"]
        self.assertEqual(page.pushButton_start_stop.toolTip(), "Pause syncing")
        page.pushButton_start_stop.click()
        instance = self.window.attached_instance("profile-a")
        self.assertTrue(wait_until(lambda: instance.props["State"] == "paused"))
        self.window.onedrive_process_status()
        self.assertEqual(page.pushButton_start_stop.toolTip(), "Resume syncing")
        page.pushButton_start_stop.click()
        self.assertTrue(wait_until(lambda: instance.props["State"] == "idle"))
        # Stop is never sent to a client the GUI did not start.
        self.window.stop_onedrive_monitor("profile-a")
        self.assertIsNone(self.mock.poll())

    def test_tray_icon_tooltip_and_menu(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        control(CONFDIR_A, "SetProp", "State", "<'offline'>")
        control(CONFDIR_A, "SetProp", "StateDetail", "<'Waiting for network'>")
        self.assertTrue(wait_until(lambda: "Waiting for network" in self.window.tray.toolTip()))
        # profile-b has no client and is stopped: stopped outranks offline.
        self.assertEqual(self.window.tray_overall_state, "stopped")
        self.assertIn("profile-b: ", self.window.tray.toolTip())

        menus = [action.text() for action in self.window.tray_menu.actions()]
        self.assertIn("profile-a", menus)
        self.assertNotIn("profile-b", menus)
        # QAction.menu() is unsafe in PySide6 (the returned wrapper deletes the menu), so find it as a child.
        submenu = next(m for m in self.window.tray_menu.findChildren(QMenu) if m.title() == "profile-a" and m.menuAction() in self.window.tray_menu.actions())
        items = [a.text() for a in submenu.actions() if not a.isSeparator()]
        info = [a.text() for a in submenu.actions() if not a.isSeparator() and not a.isEnabled()]
        self.assertEqual(info[0], "Files On-Demand")
        self.assertEqual(items[len(info):], ["Open folder", "View online", "Pause syncing", "Sync now", "Status and issues"])
        self.assertIn("Quit OneDriveGUI", menus)

    def test_status_window_issues(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        control(CONFDIR_A, "AddIssue", "Docs/report-host-safeBackup-0001.docx", "conflict_copy", "info", "Kept both")
        control(CONFDIR_A, "AddIssue", "Docs/a:b.txt", "invalid_name", "attention", "Bad name")
        control(CONFDIR_A, "SetTransfers", "[('Music/a.flac', 'hydrate', 'active', uint64 50, uint64 100), ('b', 'upload', 'queued', uint64 0, uint64 7)]")
        instance = self.window.attached_instance("profile-a")
        self.assertTrue(wait_until(lambda: len(instance.issues) >= 2 and len(instance.transfers) == 2))

        self.window.show_status_window("profile-a")
        window = self.window.status_windows["profile-a"]
        self.assertTrue(wait_until(lambda: window.group_attention.childCount() >= 1))
        self.assertGreaterEqual(window.group_handled.childCount(), 1)
        self.assertTrue(window.group_attention.text(0).startswith("Needs attention"))
        self.assertEqual(window.tree_transfers.topLevelItemCount(), 2)
        bar = window.tree_transfers.itemWidget(window.tree_transfers.topLevelItem(0), 2)
        self.assertEqual(bar.value(), 50)
        self.assertEqual(window.tree_transfers.topLevelItem(1).text(2), "Queued")

        window.group_attention.child(0).setSelected(True)
        self.assertTrue(window.button_view_online.isEnabled())
        before = window.group_attention.childCount()
        window.dismiss_issue()
        self.assertTrue(wait_until(lambda: window.group_attention.childCount() == before - 1))
        window.close()

    def test_wizard_creates_ondemand_profile_with_setup_tool(self):
        # A stand-in for onedrive-ondemand-setup that records its arguments and writes a config.
        bin_dir = os.path.join(HOME, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        fake = os.path.join(bin_dir, "onedrive-ondemand-setup")
        with open(fake, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'echo "$@" > "$HOME/setup-args"\n'
                'while [ $# -gt 0 ]; do [ "$1" = --profile ] && p=$2; [ "$1" = --mount ] && m=$2; shift; done\n'
                'mkdir -p "$HOME/.config/$p" && printf \'sync_dir = "%s"\\n\' "$m" > "$HOME/.config/$p/config"\n'
            )
        os.chmod(fake, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

        import wizard

        page = wizard.setup_wizard.page(7)
        offered = []
        page.offer_next_steps = lambda profile, unit: offered.append((profile, unit))
        page.reset()
        page.lineEdit_profile_name.setText("work-od")
        page.lineEdit_mount.setText(os.path.join(HOME, "OneDrive-work"))
        self.assertTrue(page.pushButton_create.isEnabled(), page.label_message.text())
        page.create_profile()
        self.assertTrue(wait_until(lambda: offered, timeout=10), page.label_message.text())

        self.assertEqual(offered, [("work-od", "onedrive-ondemand@work-od.service")])
        with open(os.path.join(HOME, "setup-args")) as f:
            args = f.read().split()
        self.assertEqual(args[:5], ["--non-interactive", "--profile", "work-od", "--mount", os.path.join(HOME, "OneDrive-work")])
        self.assertNotIn("--force", args)
        config = self.main_window_module.global_config["work-od"]
        self.assertEqual(config["ondemand"], "True")
        self.assertEqual(config["onedrive"]["sync_dir"].strip('"'), os.path.join(HOME, "OneDrive-work"))
        self.assertEqual(od.start_decision(config, False, False), "service")
        self.assertTrue(page.isComplete())

    def test_import_by_folder_attaches_to_running_client_at_once(self):
        # A client (non on-demand) is already running for a confdir the GUI does not know yet.
        confdir = os.path.join(HOME, ".config", "imported-c")
        os.makedirs(confdir, exist_ok=True)
        config_text = '# keep this comment\nsync_dir = "~/OneDrive-c"\n'
        with open(os.path.join(confdir, "config"), "w") as f:
            f.write(config_text)
        mock = start_mock(confdir, "--no-ondemand", "--detail", "Up to date")
        try:
            bus_name = od.bus_name_for_confdir(confdir)
            self.assertTrue(wait_until(lambda: bus_name in self.window.dbus.instances))

            import wizard

            page = wizard.setup_wizard.page(5)
            page.lineEdit_profile_name.setEnabled(True)
            page.lineEdit_config_path.setEnabled(True)
            page.lineEdit_profile_name.setText("imported-c")
            page.lineEdit_config_path.setText(os.path.join(HOME, "no-such-dir"))
            self.assertFalse(page.pushButton_import.isEnabled())
            self.assertIn("No config file found", page.label_hint.text())
            page.lineEdit_config_path.setText(confdir + "/")  # the folder, not the file
            self.assertTrue(page.pushButton_import.isEnabled())
            self.assertEqual(page.label_hint.text(), "")
            page.import_profile()

            # Matched immediately, without a restart or a D-Bus event.
            self.assertEqual(self.window.attached.get("imported-c"), bus_name)
            self.assertIn("Up to date", self.window.profile_status_pages["imported-c"].label_onedrive_status.text())
            self.assertNotIn("imported-c", self.workers)  # no client started
            self.assertEqual(self.main_window_module.global_config["imported-c"]["config_file"], os.path.join(confdir, "config"))
            # The import's save did not rewrite the running client's config.
            with open(os.path.join(confdir, "config")) as f:
                self.assertEqual(f.read(), config_text)
            self.assertFalse(os.path.exists(os.path.join(confdir, "config_backup")))
        finally:
            stop_mock(mock)
            self.main_window_module.global_config.pop("imported-c", None)
            self.window.match_dbus_instances()

    def test_import_page_still_accepts_config_file(self):
        import wizard

        page = wizard.setup_wizard.page(5)
        page.lineEdit_profile_name.setEnabled(True)
        page.lineEdit_profile_name.setText("file-path-profile")
        page.lineEdit_config_path.setText(os.path.join(CONFDIR_B, "config"))
        self.assertTrue(page.pushButton_import.isEnabled())
        self.assertEqual(page.resolved_config_path(), os.path.join(CONFDIR_B, "config"))

    def test_rematch_when_profile_config_changes(self):
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        config = self.main_window_module.global_config["profile-b"]
        original = config["config_file"]
        other = start_mock(CONFDIR_B)
        try:
            self.assertTrue(wait_until(lambda: "profile-b" in self.window.attached))
            config["config_file"] = os.path.join(HOME, ".config", "nowhere", "config")
            self.window.onedrive_process_status()
            self.assertNotIn("profile-b", self.window.attached)
            config["config_file"] = original
            self.window.onedrive_process_status()
            self.assertIn("profile-b", self.window.attached)
        finally:
            config["config_file"] = original
            stop_mock(other)
            self.assertTrue(wait_until(lambda: "profile-b" not in self.window.attached))

    def test_profile_without_client_still_spawns(self):
        decision = od.start_decision(self.main_window_module.global_config["profile-b"], "profile-b" in self.window.attached, False)
        self.assertEqual(decision, "spawn")


CLIENT_CONFIG = """# hand-written comment, keep me
sync_dir = "{mount}"
skip_file = "~*|.~*"
# second skip_file line below
skip_file = "*.tmp|*.swp|*.partial"
monitor_interval = "300"
skip_dotfiles = "false"
on_demand_thumbnails = "false"
log_dir = "/tmp/100%_logs"
"""


class ConfigGuardTests(unittest.TestCase):
    """save_global_config() and the settings page on copies of client configs in the temp HOME."""

    @classmethod
    def setUpClass(cls):
        import main_window  # noqa: F401 (builds the settings window and its pages)
        import global_config as gc
        import profile_settings_window as psw

        cls.gc = gc
        cls.psw = psw

    def make_profile(self, name, ondemand_marker):
        confdir = os.path.join(HOME, ".config", name)
        os.makedirs(confdir, exist_ok=True)
        if ondemand_marker:
            os.makedirs(os.path.join(confdir, "ondemand"), exist_ok=True)
        config_file = os.path.join(confdir, "config")
        with open(config_file, "w") as f:
            f.write(CLIENT_CONFIG.format(mount=os.path.join(HOME, name + "-mount")))
        with open(os.path.join(confdir, "sync_list"), "w") as f:
            f.write("/Documents\n# comment\n/Photos/2026\n")
        profile = {"config_file": config_file, "auto_sync": "False", "account_type": "", "free_space": ""}
        profile["onedrive"] = dict(self.gc.read_config(self.gc.DIR_PATH + "/resources/default_config")._sections["onedrive"])
        profile["onedrive"].update(self.gc.read_config(config_file)._sections["onedrive"])
        return profile

    def read(self, profile):
        with open(profile["config_file"]) as f:
            return f.read()

    def test_startup_save_skips_ondemand_and_attached_profiles(self):
        ondemand = self.make_profile("guard-ondemand", ondemand_marker=True)
        attached = self.make_profile("guard-attached", ondemand_marker=False)
        plain = self.make_profile("guard-plain", ondemand_marker=False)
        before = {name: self.read(p) for name, p in (("o", ondemand), ("a", attached), ("p", plain))}
        self.gc.set_attached_check(lambda name: name == "guard-attached")
        try:
            # plain has no '%' so the upstream rewrite can run for it
            with open(plain["config_file"], "w") as f:
                f.write(before["p"].replace("/tmp/100%_logs", "/tmp/logs"))
            plain["onedrive"]["log_dir"] = '"/tmp/logs"'
            self.gc.save_global_config({"guard-ondemand": ondemand, "guard-attached": attached, "guard-plain": plain})
        finally:
            self.gc.set_attached_check(None)
        self.assertEqual(self.read(ondemand), before["o"])
        self.assertEqual(self.read(attached), before["a"])
        self.assertFalse(os.path.exists(ondemand["config_file"] + "_backup"))
        self.assertFalse(os.path.exists(attached["config_file"] + "_backup"))
        self.assertNotIn("# hand-written comment", self.read(plain))  # upstream rewrite, unchanged behaviour

    def test_minimal_diff_save(self):
        profile = self.make_profile("guard-diff", ondemand_marker=True)
        before = self.read(profile)
        profile["onedrive"]["monitor_interval"] = '"600"'
        profile["onedrive"]["skip_dir"] = '"Cache|node_modules"'
        profile["onedrive"]["skip_dotfiles"] = '"false"'  # unchanged, equals a GUI default: must stay
        self.gc.save_global_config({"guard-diff": profile})
        after = self.read(profile)
        expected = before.replace('monitor_interval = "300"', 'monitor_interval = "600"') + 'skip_dir = "Cache|node_modules"\n'
        self.assertEqual(after, expected)
        with open(profile["config_file"] + "_backup") as f:
            self.assertEqual(f.read(), before)

        # Changing skip_file replaces its first line and drops the repeated one (value is pipe-joined).
        profile["onedrive"]["skip_file"] = '"~*|.~*|*.tmp"'
        self.gc.save_global_config({"guard-diff": profile})
        after = self.read(profile)
        self.assertEqual(after.count("skip_file"), 2)  # the comment line mentions it once
        self.assertIn('skip_file = "~*|.~*|*.tmp"\n# second skip_file line below\nmonitor_interval', after)
        self.assertIn('log_dir = "/tmp/100%_logs"', after)
        self.assertTrue(after.startswith("# hand-written comment, keep me\n"))

    def settings_page(self, name):
        profile = self.make_profile(name, ondemand_marker=True)
        global_config = self.main_window_globals()
        global_config[name] = profile
        import copy

        self.psw.temp_global_config[name] = copy.deepcopy(profile)
        page = self.psw.ProfileSettingsPage(name)
        return page, profile

    def main_window_globals(self):
        from options import global_config

        return global_config

    def test_resync_warning_for_relevant_keys_and_sync_list(self):
        from unittest import mock

        page, profile = self.settings_page("guard-page")
        sync_list = os.path.join(os.path.dirname(profile["config_file"]), "sync_list")
        with open(sync_list) as f:
            sync_list_before = f.read()
        try:
            with mock.patch.object(self.psw.QMessageBox, "question", return_value=self.psw.QMessageBox.No) as question:
                # Unchanged sync_list text round-trips through the editor and is not rewritten.
                self.assertFalse(page.sync_list_changed())
                page.temp_profile_config["onedrive"]["monitor_interval"] = '"900"'
                page.save_clicked()
                question.assert_not_called()
                self.assertIn('monitor_interval = "900"', self.read(profile))
                with open(sync_list) as f:
                    self.assertEqual(f.read(), sync_list_before)

                page.temp_profile_config["onedrive"]["sync_dir"] = '"~/Elsewhere"'
                page.save_clicked()
                question.assert_called_once()
                message = question.call_args[0][2]
                self.assertIn("sync_dir", message)
                self.assertIn("--resync", message)
                self.assertIn("systemctl --user stop onedrive-ondemand@guard-page.service", message)
                self.assertIn("--monitor --on-demand --resync --resync-auth", message)
                self.assertNotIn("Elsewhere", self.read(profile))  # declined: nothing written

                page.temp_profile_config["onedrive"]["sync_dir"] = profile["onedrive"]["sync_dir"]
                page.textEdit_sync_list.setPlainText(sync_list_before + "/Music\n")
                question.reset_mock()
                page.save_clicked()
                self.assertIn("sync_list", question.call_args[0][2])
                with open(sync_list) as f:
                    self.assertEqual(f.read(), sync_list_before)

            with mock.patch.object(self.psw.QMessageBox, "question", return_value=self.psw.QMessageBox.Yes):
                page.save_clicked()
            with open(sync_list) as f:
                self.assertEqual(f.read(), sync_list_before + "/Music\n")
        finally:
            self.main_window_globals().pop("guard-page", None)


OPTIONS_MD = """# Options in on-demand mode

Some prose.

| Option | Class | Resync | Notes |
|---|---|---|---|
| `upload_only` | refused | no | The mount needs downloads. |
| `download_only` | ignored | no | Has no effect with a mount. |
| `monitor_interval` | relevant | no | How often to look for online changes. |
| `skip_dotfiles` | risky | yes | Hidden files vanish from the mount. |
| `skip_file`, `skip_dir` | relevant-ondemand | yes | Filters what the mount shows. |
| `thing` | brandnew | maybe | Unknown class. |

| Other table | Value |
|---|---|
| `x` | y |
"""


class OptionsTableTests(unittest.TestCase):
    def test_parse_markdown(self):
        from ondemand_options import parse_options_markdown

        table = parse_options_markdown(OPTIONS_MD)
        self.assertEqual(table["upload_only"], {"class": "refused", "resync": False, "notes": "The mount needs downloads."})
        self.assertEqual(table["skip_dotfiles"]["class"], "risky")
        self.assertTrue(table["skip_dotfiles"]["resync"])
        self.assertEqual(table["skip_dir"], table["skip_file"])
        self.assertEqual(table["thing"]["class"], "unknown")
        self.assertNotIn("x", table)

    def test_generator_records_source_commit(self):
        from ondemand_options import load_options_table

        repo = tempfile.mkdtemp(dir=HOME)
        source = os.path.join(repo, "OPTIONS.md")
        with open(source, "w") as f:
            f.write(OPTIONS_MD)
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(git + ["add", "OPTIONS.md"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "options"], check=True)
        commit = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

        output = os.path.join(repo, "options.json")
        script = os.path.join(os.path.dirname(TESTS_DIR), "scripts", "generate_ondemand_options.py")
        subprocess.run([sys.executable, script, source, output], check=True, capture_output=True)
        with open(output) as f:
            data = __import__("json").load(f)
        self.assertEqual(data["source_commit"], commit)
        self.assertEqual(load_options_table(output)["upload_only"]["class"], "refused")
        self.assertEqual(load_options_table(os.path.join(repo, "missing.json")), {})


class ShippedOptionsTableTests(unittest.TestCase):
    def test_generated_table_is_complete_and_applied(self):
        from ondemand_options import CLASSES, OPTIONS_JSON, load_options_table

        with open(OPTIONS_JSON) as f:
            data = __import__("json").load(f)
        self.assertRegex(data["source_commit"], r"^[0-9a-f]{40}")
        table = load_options_table()
        self.assertGreaterEqual(len(table), 90)
        self.assertTrue(all(info["class"] in CLASSES for info in table.values()))
        self.assertEqual(table["upload_only"]["class"], "refused")
        self.assertEqual(table["sync_dir"]["class"], "relevant-ondemand")

        import main_window  # noqa: F401
        import profile_settings_window as psw

        page = psw.ProfileSettingsPage("profile-a")  # on-demand profile: real table applies
        self.assertTrue(page.checkBox_upload_only.isHidden())
        self.assertFalse(page.lineEdit_sync_dir.isHidden())
        self.assertFalse(page.checkBox_use_recycle_bin.icon().isNull())  # risky
        self.assertTrue(page.checkBox_skip_dotfiles.text().endswith("(resync)"))


class SettingsPageTests(unittest.TestCase):
    """Basic page, Advanced annotations and mode indicators, on profiles in the temp HOME."""

    @classmethod
    def setUpClass(cls):
        import main_window
        import profile_settings_window as psw

        cls.psw = psw
        cls.main_window_module = main_window
        cls.window = main_window.main_window_instance or main_window.MainWindow()
        cls.mock = start_mock(CONFDIR_A, "--syncdir", MOUNT_A)

    @classmethod
    def tearDownClass(cls):
        stop_mock(cls.mock)

    def page(self, profile):
        layout = self.psw.profile_settings_window.stackedLayout
        return next(layout.widget(i) for i in range(layout.count()) if getattr(layout.widget(i), "profile", None) == profile)

    def test_basic_tab_is_first_and_advanced_holds_upstream_editor(self):
        page = self.page("profile-a")
        tabs = page.tabWidget_basic_advanced
        self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], ["Basic", "Advanced"])
        self.assertEqual(tabs.currentIndex(), 0)
        self.assertIs(page.tabWidget.parentWidget().parentWidget().parentWidget(), tabs)

    def test_options_table_hides_marks_and_show_all(self):
        from ondemand_options import parse_options_markdown

        table = parse_options_markdown(OPTIONS_MD)
        from unittest import mock

        with mock.patch.object(self.psw, "load_options_table", return_value=table):
            page = self.psw.ProfileSettingsPage("profile-a")  # on-demand
            plain = self.psw.ProfileSettingsPage("profile-b")
        self.assertTrue(page.checkBox_upload_only.isHidden())
        self.assertTrue(page.checkBox_download_only.isHidden())
        self.assertFalse(page.checkBox_show_all_options.isHidden())
        self.assertIn("Refused in Files On-Demand mode", page.checkBox_upload_only.toolTip())
        self.assertFalse(page.checkBox_skip_dotfiles.icon().isNull())
        self.assertIn("Hidden files vanish", page.checkBox_skip_dotfiles.toolTip())
        self.assertTrue(page.checkBox_skip_dotfiles.text().endswith("(resync)"))
        self.assertIn("--resync", page.checkBox_skip_dotfiles.toolTip())
        page.checkBox_show_all_options.setChecked(True)
        self.assertFalse(page.checkBox_upload_only.isHidden())
        page.checkBox_show_all_options.setChecked(False)
        self.assertTrue(page.checkBox_upload_only.isHidden())
        # Not on-demand: upstream editor unchanged.
        self.assertFalse(plain.checkBox_upload_only.isHidden())
        self.assertTrue(plain.checkBox_show_all_options.isHidden())
        self.assertFalse(plain.checkBox_skip_dotfiles.text().endswith("(resync)"))

    def test_folder_location_change_asks_for_resync(self):
        from unittest import mock

        page = self.page("profile-a")
        basic = page.basic_page
        basic.lineEdit_folder.setText("~/Elsewhere")
        basic.folder_edited("~/Elsewhere")
        self.assertEqual(page.lineEdit_sync_dir.text(), "~/Elsewhere")
        with mock.patch.object(self.psw.QMessageBox, "question", return_value=self.psw.QMessageBox.No) as question:
            page.save_clicked()
        self.assertIn("sync_dir", question.call_args[0][2])
        page.discard_changes()

    def test_folder_selection_mirrors_sync_list_editor(self):
        page = self.page("profile-a")
        page.basic_page.textEdit_folders.setPlainText("/Documents\n")
        self.assertEqual(page.textEdit_sync_list.toPlainText(), "/Documents\n")
        page.textEdit_sync_list.setPlainText("/Photos\n")
        self.assertEqual(page.basic_page.textEdit_folders.toPlainText(), "/Photos\n")
        page.discard_changes()
        page.textEdit_sync_list.setPlainText(page.read_sync_list())

    def test_start_at_login_uses_unit_for_service_profiles_and_auto_sync_otherwise(self):
        log = os.path.join(HOME, "systemctl.log")
        basic = self.page("profile-a").basic_page
        basic.refresh()
        self.assertTrue(wait_until(lambda: "onedrive-ondemand@profile-a.service" in basic.checkBox_start_at_login.text() and basic.checkBox_start_at_login.isEnabled() or basic.refresh()))
        basic.checkBox_start_at_login.setChecked(True)
        basic.start_at_login_clicked(True)
        self.assertTrue(wait_until(lambda: os.path.exists(log) and "--user enable onedrive-ondemand@profile-a.service" in open(log).read()))
        basic.start_at_login_clicked(False)
        self.assertTrue(wait_until(lambda: "--user disable onedrive-ondemand@profile-a.service" in open(log).read()))

        plain = self.page("profile-b").basic_page
        plain.refresh()
        self.assertEqual(plain.checkBox_start_at_login.text(), "Start syncing when OneDriveGUI starts")
        before = self.page("profile-b").checkBox_auto_sync.isChecked()
        plain.start_at_login_clicked(not before)
        self.assertEqual(self.page("profile-b").checkBox_auto_sync.isChecked(), not before)
        self.page("profile-b").discard_changes()
        self.assertNotIn("profile-b", open(log).read())

    def test_free_up_space(self):
        from unittest import mock
        import ondemand_mode

        os.makedirs(os.path.join(MOUNT_A, "Documents"), exist_ok=True)
        os.makedirs(os.path.join(MOUNT_A, "Photos"), exist_ok=True)
        with open(os.path.join(MOUNT_A, "root.txt"), "w") as f:
            f.write("x")
        try:
            os.setxattr(os.path.join(MOUNT_A, "root.txt"), "user.onedrive.action", b"free")
        except OSError:
            self.skipTest("user xattrs not supported here")
        accepted, refused = ondemand_mode.free_up_space(MOUNT_A)
        self.assertEqual(accepted, ["Documents", "Photos", "root.txt"])
        self.assertEqual(os.getxattr(os.path.join(MOUNT_A, "Photos"), "user.onedrive.action"), b"free")

        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        basic = self.page("profile-a").basic_page
        basic.refresh()
        self.assertTrue(basic.pushButton_free_space.isEnabled())
        self.assertIn("Removes the local copies", basic.label_free_space.text())
        with mock.patch("basic_settings.QMessageBox.question", return_value=self.psw.QMessageBox.Yes) as question:
            basic.free_up_space_clicked()
        self.assertIn("unpinned", question.call_args[0][2])
        self.assertTrue(wait_until(lambda: "Requested for 3 item(s)" in basic.label_free_space.text()))
        self.assertFalse(self.page("profile-b").basic_page.pushButton_free_space.isVisibleTo(self.page("profile-b").basic_page))

    def test_mode_indicators(self):
        state_file = os.path.join(HOME, "systemd", "onedrive-ondemand@profile-a.service")
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            f.write("active enabled")
        self.window.unit_states.states.clear()
        self.assertTrue(wait_until(lambda: "profile-a" in self.window.attached))
        expected = "Files On-Demand - Runs as background service (systemd) - onedrive-ondemand@profile-a.service: active"
        self.assertTrue(wait_until(lambda: self.window.onedrive_process_status() or self.window.profile_status_pages["profile-a"].label_mode.toolTip() == expected))
        self.assertEqual(self.window.profile_status_pages["profile-a"].label_mode.text(), "Files On-Demand - Runs as background service (systemd)")
        index = self.window.comboBox.findText("profile-a")
        self.assertEqual(self.window.comboBox.itemData(index, Qt.ToolTipRole), expected)
        item = self.psw.profile_settings_window.listWidget_profiles.findItems("profile-a", Qt.MatchExactly)[0]
        self.assertEqual(item.toolTip(), expected)
        self.assertEqual(self.window.profile_status_pages["profile-b"].label_mode.text(), "Started by OneDriveGUI")
        self.window.show_status_window("profile-a")
        self.assertIn(expected, self.window.status_windows["profile-a"].label_details.text())
        self.window.status_windows["profile-a"].close()
        os.remove(state_file)


class QuitBehaviourTests(unittest.TestCase):
    """Runs last: it quits the application's event loop."""

    def test_closing_status_window_with_hidden_main_window_keeps_running(self):
        from unittest import mock

        import main_window

        window = main_window.MainWindow()
        window.tray = QSystemTrayIcon()
        window.tray_menu = QMenu()
        window.tray_menu_signature = None
        window.refresh_tray_menu()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            window.configure_quit_on_last_window_closed(app)
        self.assertFalse(app.quitOnLastWindowClosed())

        quit_seen = []
        app.aboutToQuit.connect(lambda: quit_seen.append(time.time()))
        events = []
        window.hide()

        def open_status():
            window.show_status_window("profile-a")
            events.append(("shown", window.status_windows["profile-a"].isVisible()))

        def close_status():
            window.status_windows["profile-a"].close()

        def check_running_then_quit():
            events.append(("running after close", not quit_seen))
            quit_action = next(a for a in window.tray_menu.actions() if a.text().startswith("Quit"))
            with mock.patch.object(main_window.QMessageBox, "question", return_value=main_window.QMessageBox.Yes):
                quit_action.trigger()

        from PySide6.QtCore import QTimer

        QTimer.singleShot(50, open_status)
        QTimer.singleShot(150, close_status)
        QTimer.singleShot(400, check_running_then_quit)
        watchdog = QTimer.singleShot(5000, lambda: events.append(("watchdog", True)) or app.exit(1))
        result = app.exec()

        self.assertEqual(events, [("shown", True), ("running after close", True)])
        self.assertEqual(result, 0)
        self.assertTrue(quit_seen)
        # The status window has the main window as parent: no taskbar entry of its own.
        self.assertIs(window.status_windows["profile-a"].parentWidget(), window)
        app.setQuitOnLastWindowClosed(True)

    def test_single_instance_server(self):
        from single_instance import InstanceServer, activate_running_instance

        name = os.path.join(HOME, "single-instance-test")
        self.assertFalse(activate_running_instance(name, timeout_ms=200))
        server = InstanceServer(name)
        requests = []
        server.activate_requested.connect(lambda: requests.append(True))
        self.assertTrue(activate_running_instance(name))
        self.assertTrue(wait_until(lambda: requests))

    def test_second_gui_start_raises_first_and_exits_and_sigterm_quits(self):
        import signal as signals

        gui = os.path.join(SRC_DIR, "OneDriveGUI.py")
        first = subprocess.Popen([sys.executable, gui], cwd=SRC_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            log = os.path.join(HOME, "onedrive-gui.log")
            def log_text():
                if not os.path.exists(log):
                    return ""
                with open(log) as f:
                    return f.read()

            self.assertTrue(wait_until(lambda: "Starting OneDriveGUI maximized" in log_text(), timeout=30))
            second = subprocess.run([sys.executable, gui], cwd=SRC_DIR, capture_output=True, text=True, timeout=60)
            self.assertEqual(second.returncode, 0)
            self.assertTrue(wait_until(lambda: "asked to show the window" in log_text(), timeout=10))
            first.send_signal(signals.SIGTERM)
            self.assertEqual(first.wait(timeout=20), 0)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()
            first.stdout.close()


def load_tests(loader, tests, pattern):
    # QuitBehaviourTests ends the application's event loop and stops the D-Bus client: run it last.
    suite = unittest.TestSuite()
    last = unittest.TestSuite()
    for test in tests:
        for case in test:
            (last if isinstance(case, QuitBehaviourTests) else suite).addTest(case)
    suite.addTests(last)
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
