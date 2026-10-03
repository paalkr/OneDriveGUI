"""
Client side of the onedrive D-Bus status interface (io.github.abraunegg.OneDrive1).

A running onedrive client that supports the interface owns the session bus name
io.github.abraunegg.OneDrive.i<first 16 hex chars of sha256(confdir)>. OneDriveGUI
discovers those instances, matches them to profiles by ConfigDir, and uses them for
status, transfers and issues instead of parsing the output of a client it spawned.

Everything here is optional: when jeepney is missing or there is no session bus,
OneDriveDBus.available is False and OneDriveGUI behaves exactly as upstream.

PySide6's QtDBus cannot demarshal structs, arrays or maps from Python (QDBusArgument's
read operators are not exposed), so the pure-Python jeepney library is used instead.
Its router runs a receiver thread; method calls run on a small thread pool; results
and signals reach the GUI thread through Qt signals.
"""

import hashlib
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal, Slot

try:
    from jeepney import DBusAddress, HeaderFields, MatchRule, message_bus, new_method_call
    from jeepney.wrappers import unwrap_msg
    from jeepney.io.threading import DBusRouter, open_dbus_connection

    JEEPNEY_AVAILABLE = True
except ImportError:
    JEEPNEY_AVAILABLE = False


BUS_NAME_PREFIX = "io.github.abraunegg.OneDrive.i"
BUS_NAME_NAMESPACE = "io.github.abraunegg.OneDrive"
OBJECT_PATH = "/io/github/abraunegg/OneDrive"
INTERFACE = "io.github.abraunegg.OneDrive1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

CALL_TIMEOUT = 5  # seconds

# Issue kinds that the client resolves by itself; used only when a client reports
# an unknown severity.
INFO_ISSUE_KINDS = {"conflict_copy", "locked_online", "deferred_online_change"}

# Tray states in priority order: the first one present in any profile wins.
TRAY_STATE_PRIORITY = ["error", "attention", "stopped", "offline", "paused", "syncing", "synced"]


# --- Pure helpers (unit tested without a bus) -----------------------------------


def bus_name_for_confdir(confdir):
    """Bus name a client running with this confdir owns (contract: sha256 of the absolute path)."""
    absolute = os.path.abspath(os.path.expanduser(confdir))
    return BUS_NAME_PREFIX + hashlib.sha256(absolute.encode()).hexdigest()[:16]


def normalize_dir(path):
    return os.path.realpath(os.path.expanduser(path.strip('"'))).rstrip("/") or "/"


def profile_confdir(profile_config):
    """confdir of a profile from OneDriveGUI's profiles file (the directory of its config file)."""
    return os.path.dirname(os.path.expanduser(profile_config["config_file"].strip('"')))


def match_profiles(profile_confdirs, instances):
    """
    Map profile names to the bus names of running instances with the same ConfigDir.

    profile_confdirs: {profile_name: confdir}
    instances: {bus_name: {"ConfigDir": ..., ...}}
    """
    by_dir = {}
    for bus_name, props in instances.items():
        config_dir = props.get("ConfigDir")
        if config_dir:
            by_dir[normalize_dir(config_dir)] = bus_name

    return {profile: by_dir[normalize_dir(confdir)] for profile, confdir in profile_confdirs.items() if normalize_dir(confdir) in by_dir}


def is_ondemand_confdir(confdir):
    """
    True if the client has run this confdir in on-demand mode: it leaves the database marker
    items.sqlite3.ondemand. <confdir>/ondemand is the backing directory of the previous layout
    (before hydrated files moved into the physical sync_dir); still checked for older profiles.
    """
    return os.path.isdir(os.path.join(confdir, "ondemand")) or os.path.exists(os.path.join(confdir, "items.sqlite3.ondemand"))


def is_ondemand_profile(profile_config):
    return str(profile_config.get("ondemand", "False")) == "True" or is_ondemand_confdir(profile_confdir(profile_config))


def profile_systemd_unit(profile_config):
    """systemd user unit of a Files On-Demand profile; derived from the confdir when not stored (imported profiles)."""
    if profile_config.get("systemd_unit"):
        return profile_config["systemd_unit"]
    confdir = profile_confdir(profile_config)
    if os.path.dirname(normalize_dir(confdir)) == normalize_dir("~/.config"):
        return systemd_unit_for_profile(os.path.basename(normalize_dir(confdir)))
    return ""


def start_decision(profile_config, attached, gui_owns_worker):
    """
    What starting sync for a profile means.

    'running': the GUI already runs a client for it.
    'attach':  a client started elsewhere (e.g. systemd) is on the bus; use it, never spawn a second one.
    'service': Files On-Demand profile that is not running; start its systemd user unit. The GUI never
               spawns `onedrive --monitor` for such a profile: without --on-demand the client would
               sync the physical sync_dir in normal mode, where online-only files are missing.
    'spawn':   upstream behaviour, the GUI starts `onedrive --monitor` itself.
    """
    if gui_owns_worker:
        return "running"
    if attached:
        return "attach"
    if is_ondemand_profile(profile_config):
        return "service"
    return "spawn"


def classify_issues(issues):
    """Split GetIssues() tuples into (handled automatically, needs attention), newest first."""
    handled, attention = [], []
    for issue in sorted(issues, key=lambda i: i[5], reverse=True):
        severity = issue[3]
        if severity == "info" or (severity != "attention" and issue[2] in INFO_ISSUE_KINDS):
            handled.append(issue)
        else:
            attention.append(issue)
    return handled, attention


def tray_state(props, issues=()):
    """Windows-like tray state of one instance: synced, syncing, paused, offline, error, attention, stopped."""
    state = props.get("State", "")
    mapped = {
        "idle": "synced",
        "syncing": "syncing",
        "starting": "syncing",
        "paused": "paused",
        "offline": "offline",
        "error": "error",
        "stopping": "stopped",
    }.get(state, "syncing")

    if mapped in ("synced", "syncing") and classify_issues(issues)[1]:
        return "attention"
    return mapped


def aggregate_tray_state(states):
    """Most important state over all profiles."""
    for state in TRAY_STATE_PRIORITY:
        if state in states:
            return state
    return "synced"


def systemd_unit_for_profile(profile_dir_name):
    """Unit that runs a Files On-Demand profile ~/.config/<profile_dir_name> (README.ondemand.md)."""
    if profile_dir_name == "onedrive-ondemand":
        return "onedrive-ondemand.service"
    return f"onedrive-ondemand@{profile_dir_name}.service"


def unwrap_variants(props):
    """jeepney returns a{sv} as {name: (signature, value)}."""
    return {name: value[1] if isinstance(value, tuple) and len(value) == 2 else value for name, value in props.items()}


def mount_move_problem(old_sync_dir, new_sync_dir):
    """
    Why the client would refuse to move an on-demand sync_dir from old to new at its next start, or "".
    It moves the physical folder with one rename(): same filesystem, target absent or empty. The old
    folder is the mountpoint while the client runs, so its filesystem is taken from its parent.
    """
    old = os.path.realpath(os.path.expanduser(old_sync_dir))
    new = os.path.realpath(os.path.expanduser(new_sync_dir))
    if os.path.isdir(new) and os.listdir(new):
        return f"{new} already contains files"
    existing = new
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    try:
        if os.stat(existing).st_dev != os.stat(os.path.dirname(old)).st_dev:
            return f"{new} is not on the same filesystem as {old}"
    except OSError as e:
        return f"cannot check {new}: {e.strerror}"
    return ""


def read_weburl(path):
    """OneDrive web URL of a path inside an on-demand mount (xattr user.onedrive.weburl)."""
    return os.getxattr(path, "user.onedrive.weburl").decode().strip("\x00").strip()


# --- Bus client -------------------------------------------------------------------


class Instance:
    def __init__(self, bus_name, unique_name, props):
        self.bus_name = bus_name
        self.unique_name = unique_name
        self.props = props
        self.transfers = []
        self.issues = []

    def has_capability(self, capability):
        return capability in self.props.get("Capabilities", [])


class OneDriveDBus(QObject):
    """
    Discovers onedrive client instances on the session bus and mirrors their state.

    Signals are always delivered on the GUI thread.
    """

    instance_added = Signal(str)  # bus_name
    instance_removed = Signal(str)  # bus_name
    properties_changed = Signal(str)  # bus_name
    transfers_changed = Signal(str)  # bus_name
    issues_changed = Signal(str)  # bus_name

    # Internal: marshal results from worker threads to the GUI thread.
    _deliver = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.instances = {}
        self.available = False
        self._router = None
        self._executor = None
        self._queue = queue.Queue()
        self._unique_to_bus = {}
        self._deliver.connect(self._on_deliver)

    # Lifecycle

    def start(self):
        """Connect to the session bus and run the initial discovery synchronously. Returns availability."""
        if not JEEPNEY_AVAILABLE:
            logging.info("[DBUS] jeepney is not installed, D-Bus integration disabled")
            return False
        try:
            self._router = DBusRouter(open_dbus_connection("SESSION"))
        except Exception as e:
            logging.info(f"[DBUS] No session bus, D-Bus integration disabled: {e}")
            self._router = None
            return False

        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="onedrive-dbus")

        rules = [
            MatchRule(type="signal", interface=PROPERTIES_INTERFACE, member="PropertiesChanged", path=OBJECT_PATH),
            MatchRule(type="signal", interface=INTERFACE, path=OBJECT_PATH),
            MatchRule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus", member="NameOwnerChanged"),
        ]
        rules[0].add_arg_condition(0, INTERFACE)
        rules[2].add_arg_condition(0, BUS_NAME_NAMESPACE, kind="namespace")
        try:
            for rule in rules:
                self._router.filter(rule, queue=self._queue)
                self._call_bus(message_bus.AddMatch(rule))
        except Exception as e:
            logging.warning(f"[DBUS] Could not subscribe to signals, D-Bus integration disabled: {e}")
            self.stop()
            return False

        self.available = True
        threading.Thread(target=self._pump, name="onedrive-dbus-signals", daemon=True).start()

        # Initial discovery is synchronous so that profile autostart already knows which
        # profiles have a running client. A local bus answers within milliseconds.
        try:
            names = self._call_bus(message_bus.ListNames())[0]
            for name in names:
                if name.startswith(BUS_NAME_PREFIX):
                    self._add_instance_sync(name)
        except Exception as e:
            logging.warning(f"[DBUS] Initial discovery failed: {e}")
        return True

    def stop(self):
        self.available = False
        if self._router is not None:
            try:
                self._router.close()
                self._router.conn.close()
            except Exception:
                pass
            self._router = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._queue.put(None)

    # Calls

    def _call_bus(self, msg, timeout=CALL_TIMEOUT):
        return unwrap_msg(self._router.send_and_get_reply(msg, timeout=timeout))

    def _call(self, bus_name, method, signature=None, body=(), interface=INTERFACE):
        address = DBusAddress(OBJECT_PATH, bus_name=bus_name, interface=interface)
        return self._call_bus(new_method_call(address, method, signature, body))

    def call_async(self, bus_name, method, signature=None, body=(), callback=None):
        """Call a contract method off the GUI thread; callback(result, error) runs on the GUI thread."""
        if not self.available:
            if callback:
                callback(None, RuntimeError("D-Bus integration not available"))
            return

        def run():
            try:
                result = self._call(bus_name, method, signature, body)
                error = None
            except Exception as e:
                result, error = None, e
                logging.warning(f"[DBUS] {method} on {bus_name} failed: {e}")
            if callback:
                self._deliver.emit(callback, (result, error))

        self._executor.submit(run)

    def run_async(self, function, callback):
        """Run a blocking function (e.g. an xattr read on a FUSE mount) off the GUI thread."""

        def run():
            try:
                result, error = function(), None
            except Exception as e:
                result, error = None, e
            self._deliver.emit(callback, (result, error))

        if self._executor is None:
            threading.Thread(target=run, daemon=True).start()
        else:
            self._executor.submit(run)

    @Slot(object, object)
    def _on_deliver(self, callback, args):
        callback(*args)

    # Contract methods

    def sync_now(self, bus_name, callback=None):
        self.call_async(bus_name, "SyncNow", callback=callback)

    def pause(self, bus_name, minutes=0, callback=None):
        self.call_async(bus_name, "Pause", "u", (minutes,), callback=callback)

    def resume(self, bus_name, callback=None):
        self.call_async(bus_name, "Resume", callback=callback)

    def dismiss_issue(self, bus_name, issue_id, callback=None):
        self.call_async(bus_name, "DismissIssue", "s", (issue_id,), callback=callback)

    # Discovery and state mirroring (worker threads)

    def _add_instance_sync(self, bus_name):
        unique_name = self._call_bus(message_bus.GetNameOwner(bus_name))[0]
        # Map the sender before fetching, so signals that arrive meanwhile are applied
        # on top of this snapshot instead of being dropped.
        self._unique_to_bus[unique_name] = bus_name
        props = unwrap_variants(self._call(bus_name, "GetAll", "s", (INTERFACE,), interface=PROPERTIES_INTERFACE)[0])
        instance = Instance(bus_name, unique_name, props)
        if instance.has_capability("transfers"):
            instance.transfers = list(self._call(bus_name, "GetTransfers")[0])
        if instance.has_capability("issues"):
            instance.issues = list(self._call(bus_name, "GetIssues")[0])
        self._deliver.emit(self._register_instance, (instance,))

    def _register_instance(self, instance):
        self.instances[instance.bus_name] = instance
        logging.info(f"[DBUS] Found client {instance.bus_name} for {instance.props.get('ConfigDir')} (state {instance.props.get('State')})")
        self.instance_added.emit(instance.bus_name)

    def _remove_instance(self, bus_name):
        instance = self.instances.pop(bus_name, None)
        if instance is None:
            return
        self._unique_to_bus.pop(instance.unique_name, None)
        logging.info(f"[DBUS] Client {bus_name} left the bus")
        self.instance_removed.emit(bus_name)

    def _pump(self):
        while True:
            msg = self._queue.get()
            if msg is None or not self.available:
                return
            try:
                self._handle_signal(msg)
            except Exception as e:
                logging.warning(f"[DBUS] Failed to handle signal: {e}")

    def _handle_signal(self, msg):
        member = msg.header.fields.get(HeaderFields.member)
        sender = msg.header.fields.get(HeaderFields.sender)

        if member == "NameOwnerChanged":
            name, old_owner, new_owner = msg.body
            if not name.startswith(BUS_NAME_PREFIX):
                return
            if old_owner:
                self._deliver.emit(self._remove_instance, (name,))
            if new_owner:
                self._add_instance_sync(name)
            return

        # Signals from an instance carry its unique name, not the well-known one.
        bus_name = self._unique_to_bus.get(sender)
        if bus_name is None:
            return

        if member == "PropertiesChanged":
            _interface, changed, invalidated = msg.body
            changed = unwrap_variants(changed)
            if invalidated:
                changed.update(unwrap_variants(self._call(bus_name, "GetAll", "s", (INTERFACE,), interface=PROPERTIES_INTERFACE)[0]))
            self._deliver.emit(self._apply_properties, (bus_name, changed))
        elif member == "TransfersChanged":
            self._fetch(bus_name, "GetTransfers", "transfers", self.transfers_changed)
        elif member == "IssuesChanged":
            self._fetch(bus_name, "GetIssues", "issues", self.issues_changed)

    def _fetch(self, bus_name, method, attribute, signal):
        result = list(self._call(bus_name, method)[0])

        def apply(bus_name, result):
            instance = self.instances.get(bus_name)
            if instance is not None:
                setattr(instance, attribute, result)
                signal.emit(bus_name)

        self._deliver.emit(apply, (bus_name, result))

    def _apply_properties(self, bus_name, changed):
        instance = self.instances.get(bus_name)
        if instance is not None:
            instance.props.update(changed)
            self.properties_changed.emit(bus_name)
