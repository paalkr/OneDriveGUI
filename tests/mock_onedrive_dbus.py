#!/usr/bin/python3
"""
Mock of the onedrive client's D-Bus status interface (io.github.abraunegg.OneDrive1).

Implements the Iteration 4 contract of the on-demand fork on the session bus so
OneDriveGUI can be developed and tested without a running client. Uses Gio from
the system python3 (python3-gi), not the GUI's venv.

    /usr/bin/python3 tests/mock_onedrive_dbus.py --confdir ~/.config/test-profile --scenario cycle

Scenarios:
    static  properties stay as given on the command line; tests drive changes
            through the extra MockControl interface.
    cycle   scripted loop: idle -> syncing (transfers progress) -> idle with new
            issues -> offline -> paused -> error -> idle ...

The extra interface io.github.abraunegg.OneDrive1.MockControl is not part of the
contract; it exists only for tests (SetProp, AddIssue, SetTransfers, CallLog, Quit).

Run it on a private bus (dbus-run-session) so it never collides with a real client.
"""

import argparse
import hashlib
import os
import sys
import time

from gi.repository import Gio, GLib

BUS_PREFIX = "io.github.abraunegg.OneDrive.i"
OBJECT_PATH = "/io/github/abraunegg/OneDrive"
INTERFACE = "io.github.abraunegg.OneDrive1"
CONTROL_INTERFACE = INTERFACE + ".MockControl"

XML = f"""
<node>
  <interface name="{INTERFACE}">
    <property name="Version" type="s" access="read"/>
    <property name="ConfigDir" type="s" access="read"/>
    <property name="SyncDir" type="s" access="read"/>
    <property name="Account" type="s" access="read"/>
    <property name="AccountType" type="s" access="read"/>
    <property name="OnDemand" type="b" access="read"/>
    <property name="Capabilities" type="as" access="read"/>
    <property name="State" type="s" access="read"/>
    <property name="StateDetail" type="s" access="read"/>
    <property name="LastSyncTime" type="x" access="read"/>
    <property name="QuotaUsed" type="t" access="read"/>
    <property name="QuotaTotal" type="t" access="read"/>
    <property name="PendingUploads" type="u" access="read"/>
    <property name="PendingDownloads" type="u" access="read"/>
    <method name="GetTransfers"><arg direction="out" type="a(ssstt)"/></method>
    <method name="GetIssues"><arg direction="out" type="a(sssssx)"/></method>
    <method name="DismissIssue"><arg direction="in" name="issueId" type="s"/></method>
    <method name="SyncNow"/>
    <method name="Pause"><arg direction="in" name="minutes" type="u"/></method>
    <method name="Resume"/>
    <signal name="IssuesChanged"/>
    <signal name="TransfersChanged"/>
  </interface>
  <interface name="{CONTROL_INTERFACE}">
    <method name="SetProp">
      <arg direction="in" name="name" type="s"/>
      <arg direction="in" name="value" type="v"/>
    </method>
    <method name="AddIssue">
      <arg direction="in" name="path" type="s"/>
      <arg direction="in" name="kind" type="s"/>
      <arg direction="in" name="severity" type="s"/>
      <arg direction="in" name="message" type="s"/>
      <arg direction="out" name="issueId" type="s"/>
    </method>
    <method name="SetTransfers"><arg direction="in" type="a(ssstt)"/></method>
    <method name="CallLog"><arg direction="out" type="as"/></method>
    <method name="Quit"/>
  </interface>
</node>
"""

PROP_TYPES = {
    "Version": "s",
    "ConfigDir": "s",
    "SyncDir": "s",
    "Account": "s",
    "AccountType": "s",
    "OnDemand": "b",
    "Capabilities": "as",
    "State": "s",
    "StateDetail": "s",
    "LastSyncTime": "x",
    "QuotaUsed": "t",
    "QuotaTotal": "t",
    "PendingUploads": "u",
    "PendingDownloads": "u",
}


def bus_name_for(confdir):
    absolute = os.path.abspath(os.path.expanduser(confdir))
    return BUS_PREFIX + hashlib.sha256(absolute.encode()).hexdigest()[:16]


class MockClient:
    def __init__(self, args, loop):
        self.loop = loop
        self.confdir = os.path.abspath(os.path.expanduser(args.confdir))
        self.connection = None
        self.props = {
            "Version": "v2.5.11-mock",
            "ConfigDir": self.confdir,
            "SyncDir": os.path.abspath(os.path.expanduser(args.syncdir)),
            "Account": args.account,
            "AccountType": args.account_type,
            "OnDemand": args.ondemand,
            "Capabilities": [c for c in args.capabilities.split(",") if c],
            "State": args.state,
            "StateDetail": args.detail,
            "LastSyncTime": 0,
            "QuotaUsed": 5 * 1024**3,
            "QuotaTotal": 1024**4,
            "PendingUploads": 0,
            "PendingDownloads": 0,
        }
        self.transfers = []
        self.issues = []
        self.issue_seq = 0
        self.call_log = []
        self.paused_until = None

    # --- helpers -----------------------------------------------------------

    def set_props(self, **changes):
        changed = {}
        for name, value in changes.items():
            if self.props.get(name) != value:
                self.props[name] = value
                changed[name] = GLib.Variant(PROP_TYPES[name], value)
        if changed and self.connection:
            self.connection.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, changed, [])),
            )

    def emit(self, signal):
        if self.connection:
            self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, signal, None)

    def add_issue(self, path, kind, severity, message):
        self.issue_seq += 1
        issue_id = f"mock-{self.issue_seq}"
        self.issues.append((issue_id, path, kind, severity, message, int(time.time())))
        self.issues = self.issues[-500:]
        self.emit("IssuesChanged")
        return issue_id

    def set_transfers(self, transfers):
        self.transfers = list(transfers)
        uploads = sum(1 for t in self.transfers if t[1] == "upload")
        self.set_props(PendingUploads=uploads, PendingDownloads=len(self.transfers) - uploads)
        self.emit("TransfersChanged")

    # --- D-Bus dispatch ----------------------------------------------------

    def on_method_call(self, connection, sender, path, interface, method, params, invocation):
        self.call_log.append(f"{method}{params.unpack() if params is not None else ''}")
        if interface == INTERFACE:
            if method == "GetTransfers":
                invocation.return_value(GLib.Variant("(a(ssstt))", (self.transfers,)))
            elif method == "GetIssues":
                invocation.return_value(GLib.Variant("(a(sssssx))", (self.issues,)))
            elif method == "DismissIssue":
                (issue_id,) = params.unpack()
                self.issues = [i for i in self.issues if i[0] != issue_id]
                self.emit("IssuesChanged")
                invocation.return_value(None)
            elif method == "SyncNow":
                if self.props["State"] == "idle":
                    self.set_props(State="syncing", StateDetail="Checking for changes")
                    GLib.timeout_add(1500, self._sync_done)
                invocation.return_value(None)
            elif method in ("Pause", "Resume"):
                if "pause" not in self.props["Capabilities"]:
                    invocation.return_dbus_error("org.freedesktop.DBus.Error.NotSupported", "pause not supported")
                    return
                if method == "Pause":
                    (minutes,) = params.unpack()
                    detail = f"Paused for {minutes} minutes" if minutes else "Paused"
                    self.set_props(State="paused", StateDetail=detail)
                else:
                    self.set_props(State="idle", StateDetail="Up to date")
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        elif interface == CONTROL_INTERFACE:
            if method == "SetProp":
                name, value = params.unpack()
                self.set_props(**{name: value})
                invocation.return_value(None)
            elif method == "AddIssue":
                issue_id = self.add_issue(*params.unpack())
                invocation.return_value(GLib.Variant("(s)", (issue_id,)))
            elif method == "SetTransfers":
                (transfers,) = params.unpack()
                self.set_transfers(transfers)
                invocation.return_value(None)
            elif method == "CallLog":
                invocation.return_value(GLib.Variant("(as)", (self.call_log,)))
            elif method == "Quit":
                invocation.return_value(None)
                GLib.idle_add(self.loop.quit)

    def on_get_property(self, connection, sender, path, interface, name):
        return GLib.Variant(PROP_TYPES[name], self.props[name])

    def _sync_done(self):
        self.set_props(State="idle", StateDetail="Up to date", LastSyncTime=int(time.time()))
        return False

    # --- scripted scenario ---------------------------------------------------

    def start_cycle(self):
        self.step = 0
        GLib.timeout_add(1000, self._cycle_tick)

    def _cycle_tick(self):
        self.step += 1
        phase = self.step % 30
        if phase == 1:
            self.set_props(State="syncing", StateDetail="Uploading 2 files, downloading 1 file")
            self.set_transfers(
                [
                    ("Documents/report.docx", "upload", "active", 0, 2_000_000),
                    ("Photos/2026/IMG_0001.jpg", "upload", "queued", 0, 4_500_000),
                    ("Music/track.flac", "hydrate", "active", 0, 30_000_000),
                ]
            )
        elif 1 < phase < 8:
            progressed = []
            for path, direction, state, done, total in self.transfers:
                done = min(total, done + total // 5)
                state = "active" if done < total else state
                progressed.append((path, direction, state, done, total))
            self.set_transfers([t for t in progressed if t[3] < t[4]])
            n = len(self.transfers)
            self.set_props(StateDetail=f"Syncing {n} file{'s' if n != 1 else ''}" if n else "Finishing sync")
        elif phase == 8:
            self.set_transfers([])
            self.set_props(State="idle", StateDetail="Up to date", LastSyncTime=int(time.time()))
            self.add_issue(
                "Documents/report-myhost-safeBackup-0001.docx",
                "conflict_copy",
                "info",
                "Kept both versions of Documents/report.docx",
            )
            self.add_issue("Shared/budget.xlsx", "locked_online", "info", "Locked online by another user; retrying")
            self.add_issue("Projects/a:b.txt", "invalid_name", "attention", "The name contains a character OneDrive does not allow")
        elif phase == 14:
            self.set_props(State="offline", StateDetail="Waiting for network")
        elif phase == 18:
            self.set_props(State="paused", StateDetail="Paused")
        elif phase == 22:
            self.set_props(State="error", StateDetail="Upload failed: quota exceeded")
            self.add_issue("Videos/big.mkv", "quota_exceeded", "attention", "Your OneDrive is full")
        elif phase == 26:
            self.set_props(State="idle", StateDetail="Up to date", LastSyncTime=int(time.time()))
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confdir", required=True)
    parser.add_argument("--syncdir", default="~/OneDrive-mock")
    parser.add_argument("--account", default="mock.user@example.com")
    parser.add_argument("--account-type", default="business")
    parser.add_argument("--ondemand", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--capabilities", default="ondemand,actions,issues,transfers,pause")
    parser.add_argument("--state", default="idle")
    parser.add_argument("--detail", default="Up to date")
    parser.add_argument("--scenario", choices=["static", "cycle"], default="static")
    parser.add_argument("--bus-name", help="override the bus name (default: derived from --confdir)")
    args = parser.parse_args()

    loop = GLib.MainLoop()
    client = MockClient(args, loop)
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    name = args.bus_name or bus_name_for(client.confdir)

    def on_bus_acquired(connection, _name):
        client.connection = connection
        for iface in node.interfaces:
            connection.register_object(OBJECT_PATH, iface, client.on_method_call, client.on_get_property, None)

    def on_name_acquired(_connection, acquired):
        print(f"mock: owning {acquired} for {client.confdir}", flush=True)
        if args.scenario == "cycle":
            client.start_cycle()

    def on_name_lost(_connection, lost):
        print(f"mock: could not own {lost}", file=sys.stderr, flush=True)
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, name, Gio.BusNameOwnerFlags.NONE, on_bus_acquired, on_name_acquired, on_name_lost)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
