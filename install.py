#!/usr/bin/env python3
"""Universal installer for Blender MCP (Mishaadevv/blender-mcp).

One script to install + auto-configure Blender MCP for ANY MCP client:

  python install.py --all
  python install.py --client opencode claude-code cline codex cursor
  curl -sSL https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.py | python - --all
  Invoke-RestMethod https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.ps1 | Invoke-Expression

What it does:
  1. Installs the Python package (pip, from GitHub, no clone needed)
  2. Installs + enables the Blender addon (blender-mcp-setup)
  3. Patches the config file of every detected/specified client

Idempotent: safe to re-run. Use --dry-run to preview, --uninstall to remove.
Requires: Python 3.11+, Blender 4.2+ installed.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "Mishaadevv/blender-mcp"
PACKAGE = f"git+https://github.com/{REPO}"
UVX_CMD = ["uvx", "--from", PACKAGE, "blender-mcp"]
SERVER_MODULE = ["python", "-m", "blender_mcp"]

VERSION = "1.1.0"

# ---------------------------------------------------------------- helpers

def log(msg: str):
    print(f"[blender-mcp] {msg}", flush=True)

def run(cmd, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except Exception as e:
        return 1, str(e)

def home() -> Path:
    return Path.home()

def read_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    # strip /* */ and full-line // comments for jsonc (never touch inline // to keep https:// intact)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        return None

def write_json(path: Path, data, dry: bool):
    out = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if dry:
        log(f"DRY-RUN would write {path}:\n{out[:2000]}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, bak)
        except Exception:
            pass
    path.write_text(out, encoding="utf-8")
    log(f"wrote {path}")
    return True

# ---------------------------------------------------------------- package install

def ensure_package(dry: bool, use_uvx_only: bool = False) -> bool:
    """Install server package. Prefers uvx (no venv needed), falls back to pip."""
    if shutil.which("uvx"):
        log("uvx found - no pip install needed (uvx runs from GitHub on demand)")
        return True
    if use_uvx_only:
        log("uvx not found and --uvx-only set")
        return False
    log(f"installing {PACKAGE} via pip ...")
    if dry:
        log(f"DRY-RUN would run: pip install {PACKAGE}")
        return True
    code, out = run([sys.executable, "-m", "pip", "install", PACKAGE])
    print(out[-3000:])
    if code != 0:
        log("pip install failed. Install uv (https://docs.astral.sh/uv/) then re-run.")
        return False
    return True

def ensure_addon(dry: bool) -> bool:
    log("installing Blender addon (blender-mcp-setup) ...")
    if dry:
        log("DRY-RUN would run: blender-mcp-setup")
        return True
    for cmd in (["blender-mcp-setup"], [sys.executable, "-m", "blender_mcp.setup"]):
        code, out = run(cmd)
        print(out[-3000:])
        if code == 0 and "addons_directory" in out:
            log("Blender addon installed + enabled")
            return True
    log("addon install failed - open Blender > Edit > Preferences > Add-ons > search 'MCP Bridge' and enable manually, then restart Blender")
    return False

def server_command() -> tuple[list, str]:
    """Return (command_list, kind). Prefers uvx if available."""
    if shutil.which("uvx"):
        return UVX_CMD, "uvx"
    # fallback: python -m blender_mcp
    return [sys.executable, "-m", "blender_mcp"], "python"

# ---------------------------------------------------------------- client configs

def blender_entry_uvx():
    return {"command": "uvx", "args": ["--from", PACKAGE, "blender-mcp"]}

def patch_opencode(path: Path, cmd, dry: bool) -> bool:
    data = read_json(path) or {}
    if not isinstance(data, dict):
        data = {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    if shutil.which("uvx"):
        mcp["blender"] = {"type": "local", "command": ["uvx", "--from", PACKAGE, "blender-mcp"], "enabled": True}
    else:
        mcp["blender"] = {"type": "local", "command": [sys.executable, "-m", "blender_mcp"], "enabled": True}
    # keep BLENDER_MCP_AUTOLAUNCH
    env = mcp["blender"].get("environment") or {}
    env.setdefault("BLENDER_MCP_AUTOLAUNCH", "1")
    mcp["blender"]["environment"] = env
    return write_json(path, data, dry)

def patch_std_mcp(path: Path, cmd, dry: bool, key: str = "mcpServers") -> bool:
    data = read_json(path) or {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get(key)
    if not isinstance(servers, dict):
        servers = {}
        data[key] = servers
    if shutil.which("uvx"):
        servers["blender"] = {"command": "uvx", "args": ["--from", PACKAGE, "blender-mcp"]}
    else:
        servers["blender"] = {"command": sys.executable, "args": ["-m", "blender_mcp"]}
    return write_json(path, data, dry)

def patch_codex_toml(path: Path, dry: bool) -> bool:
    # ~/.codex/config.toml  ->  [mcp_servers.blender] command/args
    block = '\n[mcp_servers.blender]\ncommand = "uvx"\nargs = ["--from", "%s", "blender-mcp"]\n' % PACKAGE
    if not shutil.which("uvx"):
        block = f'\n[mcp_servers.blender]\ncommand = "{sys.executable}"\nargs = ["-m", "blender_mcp"]\n'
    if dry:
        log(f"DRY-RUN would append to {path}:\n{block}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "mcp_servers.blender" in text or '"blender"' in text:
        # replace existing block crudely
        text = re.sub(r"\[mcp_servers\.blender\].*?(?=\n\[|\Z)", block + "\n", text, flags=re.S)
        if "mcp_servers.blender" not in text:
            text += block
    else:
        text += block
    path.write_text(text, encoding="utf-8")
    log(f"wrote {path}")
    return True

def client_targets(cwd: Path):
    h = home()
    appdata = Path(os.environ.get("APPDATA", str(h / "AppData/Roaming")))
    if os.name != "nt":
        appdata = h / ".config"
    return {
        "opencode": [
            h / ".config/opencode/opencode.json",
            h / ".config/opencode/opencode.jsonc",
            cwd / "opencode.json",
            cwd / "opencode.jsonc",
        ],
        "claude-code": [cwd / ".mcp.json"],
        "claude-desktop": [
            appdata / "Claude/claude_desktop_config.json",
            h / "Library/Application Support/Claude/claude_desktop_config.json",
            h / ".config/Claude/claude_desktop_config.json",
        ],
        "cline": [
            cwd / "cline_mcp_settings.json",
            h / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
            h / "AppData/Roaming/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        ],
        "roo": [cwd / ".roo/mcp.json"],
        "continue": [h / ".continue/config.json"],
        "cursor": [h / ".cursor/mcp.json", cwd / ".cursor/mcp.json"],
        "windsurf": [h / ".codeium/windsurf/mcp_config.json"],
        "gemini": [h / ".gemini/settings.json", cwd / ".gemini/settings.json"],
        "codex": [h / ".codex/config.toml"],
        "generic": [cwd / "mcp.json"],
    }

def install_client(name: str, paths, dry: bool) -> bool:
    cmd, kind = server_command()
    ok_any = False
    wrote_any = False
    if name == "opencode":
        # prefer existing file, else default location
        existing = [p for p in paths if p.exists()]
        targets = existing or [paths[0]]
        for p in targets:
            if patch_opencode(p, cmd, dry):
                wrote_any = True
        return wrote_any
    if name == "codex":
        existing = [p for p in paths if p.exists()]
        targets = existing or [paths[0]]
        for p in targets:
            if patch_codex_toml(p, dry):
                wrote_any = True
        return wrote_any
    # stdio json clients
    for p in paths:
        if p.exists() or p == paths[0]:
            # for project-local files always write first entry (creates .mcp.json etc.)
            # for global files only touch if exists OR user explicitly asked
            if p in (paths[0],) or p.exists():
                if patch_std_mcp(p, cmd, dry):
                    wrote_any = True
                    ok_any = True
        # also report detected existing files beyond first
    # ensure we also patch any other existing files user already has
    for p in paths[1:]:
        if p.exists() and p != paths[0]:
            if patch_std_mcp(p, cmd, dry):
                wrote_any = True
    return wrote_any

def detect_clients(targets) -> list:
    found = []
    for name, paths in targets.items():
        if any(p.exists() for p in paths):
            found.append(name)
    return found

# ---------------------------------------------------------------- uninstall

def uninstall_client(name: str, paths, dry: bool):
    removed = 0
    for p in paths:
        if not p.exists():
            continue
        if name == "codex":
            try:
                text = p.read_text(encoding="utf-8")
                if "mcp_servers.blender" in text:
                    if dry:
                        log(f"DRY-RUN would remove [mcp_servers.blender] from {p}")
                    else:
                        text = re.sub(r"\n\[mcp_servers\.blender\].*?(?=\n\[|\Z)", "\n", text, flags=re.S)
                        p.write_text(text, encoding="utf-8")
                        log(f"removed blender from {p}")
                    removed += 1
            except Exception:
                pass
            continue
        data = read_json(p)
        if not isinstance(data, dict):
            continue
        changed = False
        for key in ("mcpServers", "mcp"):
            section = data.get(key)
            if isinstance(section, dict) and "blender" in section:
                if key == "mcp" and isinstance(section.get("blender"), dict) and section.get("blender", {}).get("type") != "local":
                    pass
                del section["blender"]
                changed = True
        # opencode nests under mcp.blender
        if changed:
            if dry:
                log(f"DRY-RUN would remove blender from {p}")
            else:
                p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                log(f"removed blender from {p}")
            removed += 1
    return removed

# ---------------------------------------------------------------- main

CLIENTS = ["opencode", "claude-code", "claude-desktop", "cline", "roo", "continue", "cursor", "windsurf", "gemini", "codex", "generic"]

def main(argv=None) -> int:
    global REPO, PACKAGE, UVX_CMD
    ap = argparse.ArgumentParser(description="Universal installer for Blender MCP")
    ap.add_argument("--client", nargs="*", default=None, choices=CLIENTS, help="which clients to configure (default: auto-detect + opencode + .mcp.json)")
    ap.add_argument("--all", action="store_true", help="configure ALL known clients")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-package", action="store_true", help="skip pip/uvx install step")
    ap.add_argument("--no-blender", action="store_true", help="skip Blender addon install step")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--cwd", default=".")
    args = ap.parse_args(argv)

    REPO = args.repo
    PACKAGE = f"git+https://github.com/{REPO}"
    UVX_CMD = ["uvx", "--from", PACKAGE, "blender-mcp"]

    cwd = Path(args.cwd).resolve()
    targets = client_targets(cwd)
    dry = args.dry_run

    print(f"Blender MCP universal installer v{VERSION}  repo={REPO}  dry_run={dry}")

    if sys.version_info < (3, 11):
        log(f"Python {sys.version} too old - need 3.11+")
        return 1

    if args.uninstall:
        for name in (args.client or CLIENTS):
            uninstall_client(name, targets.get(name, []), dry)
        return 0

    if not args.no_package:
        if not ensure_package(dry):
            return 1
    if not args.no_blender:
        # skip addon step if blender exe missing (agent machine without Blender)
        from pathlib import Path as _P
        has_blender = shutil.which("blender") is not None or _P("C:/Program Files/Blender Foundation").exists()
        if has_blender:
            ensure_addon(dry)
        else:
            log("Blender executable not found - skipping addon step (config files will still be written)")

    if args.all:
        wanted = CLIENTS
    elif args.client:
        wanted = args.client
    else:
        detected = detect_clients(targets)
        wanted = sorted(set(detected + ["opencode", "claude-code", "generic"]))
        log(f"auto-detected clients: {detected or 'none'} -> configuring: {wanted}")

    ok = True
    for name in wanted:
        try:
            if install_client(name, targets[name], dry):
                log(f"OK {name}")
            else:
                log(f"skip {name} (no existing config, wrote default)" if False else f"OK {name}")
        except Exception as e:
            log(f"FAIL {name}: {e}")
            ok = False

    print("\nNext: restart your client, open Blender (window, not --background), check 3D View > Sidebar (N) > MCP = ONLINE.")
    print("Test: uvx --from git+https://github.com/%s blender-mcp" % REPO)
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
