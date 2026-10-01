# OneDriveGUI and the onedrive D-Bus status interface

Design note for the `ondemand/dbus-gui` branch. The interface itself is specified in the
on-demand client fork (`ondemand/CONTRACT.md`, "Iteration 4"): bus name
`io.github.abraunegg.OneDrive.i<16 hex of sha256(confdir)>`, object `/io/github/abraunegg/OneDrive`,
interface `io.github.abraunegg.OneDrive1`.

## How upstream OneDriveGUI works

**Profiles.** `~/.config/onedrive-gui/profiles` is an INI file with one section per profile:
`config_file` (path of the client's config file; its directory is the `--confdir`), `auto_sync`,
`account_type`, `free_space`. `global_config.create_global_config()` loads it at import time
(`options.global_config`) and merges each profile's client config, read with `read_config()`
(the client's file has no section header), over `resources/default_config` into
`global_config[profile]["onedrive"]`. `save_global_config()` writes the profiles file and rewrites
every profile's client config without default-valued keys (keeping `config_backup`). It runs at
every GUI start (`OneDriveGUI.py`) and when settings are saved.

**Client process.** `workers.WorkerThread` (one `QThread` per profile, registered in
`workers.workers`) runs `exec onedrive --confdir=<dir> --monitor -v` through a shell and parses
stdout line by line (`read_stdout`): status messages, transfer lines ("Uploading file ...",
"... 80% | ETA"), errors (multi-line `ERROR:` blocks, "Failed to upload"), free space, account
type, and prompts (login, `--resync`, big delete). It emits `update_profile_status`,
`update_progress_new`, `update_credentials`, `trigger_resync`, ... to `MainWindow`.
`stop_worker()` kills the process. `MaintenanceWorker` runs one-shot client commands (login with
`--auth-response`, SharePoint queries).

**Status and tray.** `MainWindow` keeps a `ProfileStatusPage` per profile (status label, account
type, free space, start/stop button, transfer list of `TaskList` widgets). A 500 ms timer
(`onedrive_process_status`) sets the running/stopped light and rewires the start/stop button from
`workers`, then `update_tray_icon()` derives one of ERROR/STOPPED/SYNCING/IDLE by matching words
in the status label text, and picks the bundled icons8 cloud icons. The tray menu is
Show/Hide, Settings, Quit; Quit stops every worker.

## Where D-Bus plugs in

New modules, all optional at runtime:

- `src/ondemand_dbus.py`: bus client and pure helpers (bus name, profile matching, start
  decision, issue classification, tray state mapping). `OneDriveDBus.start()` returns False when
  jeepney is missing or there is no session bus; `MainWindow.dbus` is then `None` and every new
  path is skipped.
- `src/ondemand_ui.py`: themed tray icons per state and the status window (transfers, issues).
- `src/ondemand_profile.py`: Files On-Demand profile creation through `onedrive-ondemand-setup`.

**Library choice.** PySide6's QtDBus is installed with PySide6_Essentials, but from Python it
cannot read the contract's types: replies with `a{sv}`, `a(ssstt)` and `a(sssssx)` arrive as
`QDBusArgument` objects whose read operators (`beginStructure()` and friends) bind to the write
overloads ("QDBusArgument: write from a read-only object"); string-based `QDBusConnection.connect`
slots also failed. Gio/PyGObject would need the system `python3-gi` in the venv
(`--system-site-packages`) and the AppImage, and relies on Qt running the GLib main loop. So the
GUI uses **jeepney** (pure Python, no compiled parts, `pip install jeepney`): its threading router
receives on its own thread, method calls run on a two-thread pool, and results reach the GUI
thread through a queued Qt signal. Nothing blocks the GUI thread except the initial discovery
(`ListNames`, then `GetAll`/`GetTransfers`/`GetIssues` per instance, 5 s timeout), which is
synchronous on purpose so profile autostart already knows which profiles have a running client.

**Discovery.** Initial `ListNames` filtered by the prefix, then `NameOwnerChanged`
(arg0namespace `io.github.abraunegg.OneDrive`) for clients that start or stop later. Signals are
subscribed with match rules for `PropertiesChanged` (arg0 = our interface), the interface's own
signals, and mapped from the sender's unique name to the instance. `TransfersChanged` and
`IssuesChanged` trigger `GetTransfers()` / `GetIssues()`.

**Profile matching.** An instance belongs to a profile when its `ConfigDir` property equals the
directory of the profile's `config_file` (both `realpath`ed). The bus-name hash is not used for
matching, only `ConfigDir`.

**Attach instead of spawn.** `start_decision()` decides what "start sync" means:
`running` (the GUI already has a worker), `attach` (a client for this confdir is on the bus:
never start a second one), `service` (a Files On-Demand profile that is not running: offer to
`systemctl --user start` its unit), `spawn` (upstream). A profile counts as Files On-Demand when the
profiles file says `ondemand = True`, or when its confdir has the client's on-demand markers
(`ondemand/` or `items.sqlite3.ondemand`), so an imported on-demand profile is never started as
`onedrive --monitor` without `--on-demand`. Autostart only acts on `spawn`. For an attached profile
the GUI never stops the client: the start/stop button becomes pause/resume (if the `pause`
capability is present, otherwise it is disabled), Quit says which clients keep running, and only
`workers` are stopped. A client started by the GUI that also exposes D-Bus keeps its worker
(log parsing, stop) and additionally feeds the tray state from D-Bus.

**Tray.** With at least one attached profile, `update_attached_tray_icon()` replaces the
word-matching logic: per profile `tray_state()` maps `State` to synced, syncing, paused, offline,
error or stopped, and to attention when an `attention` issue exists while synced or syncing;
profiles without a client keep the upstream classification. The most important state wins
(error, attention, stopped, offline, paused, syncing, synced). Icons come from the icon theme
(all exist in Yaru and Adwaita): `weather-overcast-symbolic` (synced, a cloud),
`emblem-synchronizing-symbolic`, `media-playback-pause-symbolic`, `network-offline-symbolic`,
`dialog-error-symbolic`, `dialog-warning-symbolic`, `process-stop-symbolic`, with the bundled
icons8 PNGs as fallback. Tooltip: `StateDetail` (plus the number of issues needing attention), one
line per profile when there are several. The menu gets, per attached profile (a submenu when there
are several profiles): Open folder, View online (on-demand only, `user.onedrive.weburl` of
`SyncDir`, read off the GUI thread), Pause/Resume syncing (capability), Sync now, Status and issues;
then the upstream entries, with Quit renamed "Quit OneDriveGUI". Without attached profiles the
menu and icons are exactly upstream.

**Status window** (`OnDemandStatusWindow`, from the tray or the new info button on the profile
page, shown only for attached profiles): state line, account, last sync, quota; Sync now,
Pause/Resume, Open folder; tab Transfers (path, direction, progress bar or "Queued", size) and tab
Issues with the groups "Needs attention" and "Handled automatically" (by `severity`, by `kind` when
the severity is unknown) and the actions Open file, Open folder, View online (xattr, on-demand
only, async), Dismiss (`DismissIssue`). The profile page's transfer list shows the attached
client's transfers too (from `GetTransfers()`, icons by file name only so online-only files are
never read); a transfer that leaves the list is shown as finished, or failed when an attention
issue exists for its path.

**Profile setup.** The wizard offers "Create new Files On-Demand profile" when
`onedrive-ondemand-setup` is installed. It runs
`onedrive-ondemand-setup --non-interactive --profile <name> --mount <dir> --no-auth --no-enable-service`
(plus `--business --azure-tenant-id/--application-id` when given; never `--force`), then adds the
profile to OneDriveGUI's profiles file with `ondemand = True` and
`systemd_unit = onedrive-ondemand@<name>.service` (`onedrive-ondemand.service` for the default
profile name). A dialog then lists the remaining steps from `README.ondemand.md` (sign in, first
synchronisation with `--resync --resync-auth` in a terminal, enable the unit) with buttons "Sign in
now" (the GUI's existing login window) and "Enable service now"
(`systemctl --user daemon-reload && systemctl --user enable --now <unit>`). Folder selection and
settings use the existing editors.

## Caveats

- `save_global_config()` rewrites the client config of every GUI profile at each GUI start
  (upstream behaviour, with `config_backup`). For client-owned profiles (Files On-Demand, or
  attached to a running client) it does not: only keys whose value differs from the file (or from
  the default when absent) are written, by editing the file line by line (comments, order,
  unknown keys and repeated `skip_file`/`skip_dir` lines stay; values are raw, no interpolation);
  unchanged profiles are not touched at all. The startup save now runs after `MainWindow`, so D-Bus
  discovery has happened. Saving settings of such a profile asks for confirmation when a key the
  client treats as resync-relevant (`applicationChangeWhereResyncRequired()`: drive_id, sync_dir,
  skip_file, skip_dir, skip_dotfiles, skip_symlinks, sync_business_shared_items, check_nosync,
  skip_size) or `sync_list` changes, and shows the stop / `--resync` / start commands for the unit.
  The client hashes the whole `sync_list` file (QuickXorHash), so it is only written when its text
  changed.
- The tray uses symbolic icons; how they render (colour, size) depends on the tray host
  (AppIndicator/StatusNotifier on GNOME) and needs checking on the desktop.
- The GUI's transfer history for attached clients only knows what `GetTransfers()` showed while
  the GUI ran; a finished transfer is inferred from its disappearance.

## Testing

`tests/run_tests.sh` runs `tests/test_ondemand.py` with a private session bus
(`dbus-run-session`), a temporary HOME holding two GUI profiles, and `QT_QPA_PLATFORM=offscreen`.
`tests/mock_onedrive_dbus.py` implements the contract with Gio from the system python3
(`--scenario static` driven through a test-only `MockControl` interface, or `--scenario cycle`
for a scripted loop of syncing, transfers, issues, offline, paused and error).
