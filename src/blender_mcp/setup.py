"""Install the bundled Blender addon into the user's Blender and enable it.

This is what makes the server work on a machine that has never seen the addon:
one call copies the sources into Blender's user addons directory and then lets
Blender itself enable the addon and persist the preference.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE = "blender_mcp_bridge"
ADDON_SOURCE = Path(__file__).resolve().parent / "addon" / MODULE


class SetupError(RuntimeError):
    """Anything the user could act on to fix an install problem."""


# --------------------------------------------------------------------------- #
# locating Blender
# --------------------------------------------------------------------------- #


def blender_executable() -> str | None:
    """Best effort search for blender.exe / blender across platforms."""
    override = os.environ.get("BLENDER_EXE")
    if override and Path(override).is_file():
        return override

    home = Path.home()
    candidates: list[Path] = []

    if os.name == "nt":
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramW6432", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for root in program_files:
            if not root:
                continue
            base = Path(root) / "Blender Foundation"
            if base.is_dir():
                candidates += [entry / "blender.exe" for entry in sorted(base.iterdir(), reverse=True)]
            local = Path(root) / "Programs"
            if local.is_dir():
                candidates += [entry / "blender.exe" for entry in sorted(local.iterdir(), reverse=True)]
    else:
        for pattern in ("/Applications/Blender*.app/Contents/MacOS/Blender",
                        "/usr/bin/blender", "/usr/local/bin/blender",
                        "/snap/bin/blender", str(home / ".local/bin/blender")):
            if "*" in pattern:
                base, _, name = pattern.rpartition("/")
                if Path(base).is_dir():
                    candidates += [Path(base) / entry / name
                                   for entry in sorted(os.listdir(base), reverse=True)]
            else:
                candidates.append(Path(pattern))
        flatpak = home / ".local/share/flatpak/exports/bin/blender"
        if flatpak.is_file():
            candidates.append(flatpak)

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    from shutil import which

    return which("blender")


def blender_version(executable: str) -> str:
    """Return the major.minor string Blender uses for its user directories."""
    out = subprocess.run([executable, "--version"], capture_output=True,
                         text=True, check=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == "Blender" and len(parts) > 1:
            match = re.match(r"(\d+\.\d+)", parts[1])
            if match:
                return match.group(1)
    raise SetupError(f"Could not read the Blender version from:\n{out}")


def user_addons_dir(executable: str) -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.environ.get(
            "XDG_CONFIG_HOME", Path.home() / ".config"))
    target = base / "Blender Foundation" / "Blender" / blender_version(executable) / "scripts" / "addons"
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #


def addon_installed(executable: str) -> bool:
    return (user_addons_dir(executable) / MODULE / "__init__.py").is_file()


def install_addon(executable: str | None = None, enable: bool = True) -> dict:
    """Copy the bundled addon into Blender's user addons directory.

    Returns a report dict. Raises :class:`SetupError` with an actionable message
    when something goes wrong.
    """
    if not ADDON_SOURCE.is_dir():
        raise SetupError(
            f"The bundled addon is missing from this installation "
            f"(expected at {ADDON_SOURCE}). Reinstall the package."
        )
    executable = executable or blender_executable()
    if not executable:
        raise SetupError(
            "Could not find Blender. Set the BLENDER_EXE environment variable "
            "to the full path of your Blender executable and try again."
        )

    target = user_addons_dir(executable) / MODULE
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ADDON_SOURCE, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    report = {
        "blender": executable,
        "blender_version": blender_version(executable),
        "addons_directory": str(target),
        "files": sorted(p.name for p in target.glob("*.py")),
        "enabled": False,
    }
    if not enable:
        return report

    # Let Blender enable the addon and write the preference itself, so the
    # enabled-addon list is exactly what Blender would have produced.
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "enable_addon.py"
        script.write_text(
            "import addon_utils, bpy\n"
            f"module = {MODULE!r}\n"
            "if module not in bpy.context.preferences.addons:\n"
            "    bpy.ops.preferences.addon_enable(module=module)\n"
            "else:\n"
            "    addon_utils.enable(module, default_set=True, persistent=True)\n"
            "assert module in bpy.context.preferences.addons, 'addon failed to enable'\n"
            "bpy.ops.wm.save_userpref()\n"
            "print('MCPADDON_ENABLED')\n",
            encoding="utf-8",
        )
        # Deliberately NOT --factory-startup: Blender must load the user's
        # existing preferences, otherwise saving them back would silently
        # disable every add-on they had enabled.
        result = subprocess.run(
            [executable, "--background", "--python-exit-code", "1",
             "--python", str(script)],
            capture_output=True, text=True,
        )
    report["enabled"] = "MCPADDON_ENABLED" in result.stdout
    if not report["enabled"]:
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        raise SetupError(
            "The addon was copied but Blender refused to enable it:\n"
            + "\n".join(tail)
            + "\n\nEnable it manually: Blender > Edit > Preferences > Add-ons > "
              "search 'MCP Bridge'."
        )
    return report


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-enable" in argv:
        argv.remove("--no-enable")
        enable = False
    else:
        enable = True
    try:
        report = install_addon(enable=enable)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print("\nRestart Blender, then the bridge listens on 127.0.0.1:9876.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
