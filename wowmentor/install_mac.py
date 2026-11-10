"""Compatibility shim: the installer was split into wowmentor.install (neutral) and wowmentor.host.darwin (macOS only).

Old imports such as `from wowmentor import install_mac` keep working for one release; new code should import the real modules.
"""
from .install import (POOL, ADDON, PKG, PKG_ROOT, find_wow_dirs, addons_dir, valid_wav, install_addon, create_slots,  # noqa: F401
                      remove_addon, slots_present, addon_installed_version)
from .host.darwin import (LABELS, APP_NAME, label, have_tool, build_mask, LAUNCHER_C, c_string, launcher_source, app_path,  # noqa: F401
                          build_launcher, plist_path, make_plist, write_plist, launchctl, start_agent, stop_agent, agent_running,
                          remove_plist, open_screen_recording_settings)
