"""
Files On-Demand profiles: created with onedrive-ondemand-setup (shipped by the
onedrive-ondemand package) and run by the systemd user unit
onedrive-ondemand@<profile>.service instead of a GUI-managed process.
"""

import os
import re
import shutil
from configparser import ConfigParser

from ondemand_dbus import systemd_unit_for_profile

SETUP_TOOL = "onedrive-ondemand-setup"
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")


def setup_tool_path():
    """Path of onedrive-ondemand-setup, or None when the on-demand client is not installed."""
    return shutil.which(SETUP_TOOL)


def profile_dir(profile_name):
    return os.path.expanduser(f"~/.config/{profile_name}")


def setup_command(profile_name, mount, azure_tenant_id="", application_id=""):
    """Arguments for a non-interactive onedrive-ondemand-setup run that writes the profile only.

    Never passes --force (an existing, different profile is refused, not overwritten) and never
    enables the unit; signing in and enabling are separate, explicit steps in the GUI.
    """
    args = ["--non-interactive", "--profile", profile_name, "--mount", mount, "--no-auth", "--no-enable-service"]
    if azure_tenant_id or application_id:
        args.append("--business")
    if azure_tenant_id:
        args += ["--azure-tenant-id", azure_tenant_id]
    if application_id:
        args += ["--application-id", application_id]
    return args


def gui_profile_entry(profile_name):
    """Section for ~/.config/onedrive-gui/profiles describing a Files On-Demand profile."""
    return {
        "config_file": os.path.join(profile_dir(profile_name), "config"),
        "auto_sync": "False",
        "account_type": "",
        "free_space": "",
        "ondemand": "True",
        "systemd_unit": systemd_unit_for_profile(profile_name),
    }


def add_gui_profile(profiles_file, profile_name):
    """Append the profile to OneDriveGUI's profiles file and return its section."""
    entry = gui_profile_entry(profile_name)
    profiles = ConfigParser()
    profiles.read(profiles_file)
    profiles[profile_name] = entry
    os.makedirs(os.path.dirname(profiles_file), exist_ok=True)
    with open(profiles_file, "w") as f:
        profiles.write(f)
    return entry


def validate_new_profile(profile_name, mount, used_profile_names, used_sync_dirs):
    """Return an error message, or "" when the profile can be created."""
    if not PROFILE_NAME_PATTERN.match(profile_name):
        return "Use letters, digits, '.', '_', '-' or '@' for the profile name."
    if profile_name in used_profile_names:
        return f"OneDriveGUI already has a profile named {profile_name}."
    if not mount.strip():
        return "Choose the folder where OneDrive should appear."
    expanded = os.path.realpath(os.path.expanduser(mount))
    if expanded in {os.path.realpath(os.path.expanduser(d)) for d in used_sync_dirs}:
        return "This folder is already used by another profile."
    if os.path.ismount(expanded):
        return "This folder is already a mount point (another client may be using it)."
    if os.path.isdir(expanded) and os.listdir(expanded):
        return "The folder must be empty or not exist yet, because the mount hides what is in it."
    return ""
