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

from PySide6.QtCore import QCoreApplication
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
        self.assertIn("profile-b: OneDrive sync is not running", self.window.tray.toolTip())

        menus = [action.text() for action in self.window.tray_menu.actions()]
        self.assertIn("profile-a", menus)
        self.assertNotIn("profile-b", menus)
        # QAction.menu() is unsafe in PySide6 (the returned wrapper deletes the menu), so find it as a child.
        submenu = next(m for m in self.window.tray_menu.findChildren(QMenu) if m.title() == "profile-a" and m.menuAction() in self.window.tray_menu.actions())
        items = [a.text() for a in submenu.actions()]
        self.assertEqual(items, ["Open folder", "View online", "Pause syncing", "Sync now", "Status and issues"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
