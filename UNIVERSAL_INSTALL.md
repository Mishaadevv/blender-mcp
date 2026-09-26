# Blender MCP — Universal Install (Mishaadevv/blender-mcp)

One repo, one command, any client. No manual JSON editing.

## 0. Requirements
- Python 3.11+
- Blender 4.2+ (4.5 LTS recommended), installed normally (not snap --background only)
- `uvx` recommended (`pip install uv` or https://docs.astral.sh/uv/), fallback is plain `pip`

## 1. One-line auto-install (agent or terminal)

**Python (all platforms):**
```bash
curl -sSL https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.py -o /tmp/bm.py && python /tmp/bm.py --all
# Windows PowerShell:
# irm https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.py -OutFile $env:TEMP/bm.py; python $env:TEMP/bm.py --all
```

**Linux/macOS:**
```bash
curl -sSL https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.sh | bash -s -- --all
```

**Windows PowerShell:**
```powershell
irm https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.ps1 | iex
# or with explicit args:
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.ps1))) --all
```

**Agent prompt (paste to any agent):**
```
Download https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.py and run `python install.py --all`, then verify `blender-mcp-setup` succeeded.
```

## 2. Selective install
```bash
python install.py --client opencode claude-code cline codex
python install.py --dry-run --all        # preview only
python install.py --uninstall            # remove blender entries
python install.py --no-blender           # configure clients on a machine without Blender
python install.py --repo MyOrg/my-fork   # use a fork
```

## 3. What the script does
1. `pip install git+https://github.com/Mishaadevv/blender-mcp` (skipped if `uvx` exists — uvx runs directly from GitHub)
2. `blender-mcp-setup` — copies `blender_mcp_bridge` addon into Blender user addons + enables it via Blender itself
3. Patches configs (creates backup `*.bak`, merges, never wipes other servers):
   - **opencode** — `~/.config/opencode/opencode.json[c]`, `./opencode.json[c]` → `{"mcp":{"blender":{"type":"local","command":["uvx",...],"enabled":true,"environment":{"BLENDER_MCP_AUTOLAUNCH":"1"}}}}`
   - **claude-code** — `./.mcp.json` → `{"mcpServers":{"blender":{"command":"uvx","args":[...]}}}`
   - **claude-desktop** — `%APPDATA%/Claude/claude_desktop_config.json` (win) / `~/Library/Application Support/Claude/...` (mac) / `~/.config/Claude/...` (linux)
   - **cline / Roo / Continue / Cursor / Windsurf / Gemini** — their `*_mcp_settings.json` / `mcp.json` / `config.json` / `settings.json` equivalents
   - **codex** — `~/.codex/config.toml` → `[mcp_servers.blender]`
   - **generic** — `./mcp.json` (any stdio client: freebuff, opencode, cline, codex, etc.)
4. Prints next steps.

## 4. Manual fallback (no script)
```bash
uvx --from git+https://github.com/Mishaadevv/blender-mcp blender-mcp
blender-mcp-setup
```
Then copy `.mcp.json` from this repo into your project, or the `opencode` snippet from README.

## 5. Verify
- Open Blender (normal window, NOT `--background`) → `3D View > Sidebar (N) > MCP` = **ONLINE 127.0.0.1:9876**
- `echo '{"id":"ping","command":"ping","params":{},"timeout":5}' | nc 127.0.0.1 9876`
- In client: MCP tools list should show 46 `blender_*` tools.

## 6. Push to your own GitHub (Mishaadevv)
```bash
cd blender-mcp
git remote add origin git@github.com:Mishaadevv/blender-mcp.git
# or: git remote add origin https://github.com/Mishaadevv/blender-mcp.git
git branch -M main
git add install.py install.sh install.ps1 .mcp.json UNIVERSAL_INSTALL.md pyproject.toml README.md
git commit -m "Universal installer: one-line setup for opencode/cline/claude-code/codex/all"
git push -u origin main
```

## 7. Supported clients matrix
| Client | Config file | Format |
|---|---|---|
| opencode | `~/.config/opencode/opencode.json[c]` | `mcp.blender (type=local)` |
| claude-code | `./.mcp.json` | `mcpServers.blender` |
| claude-desktop | `claude_desktop_config.json` | `mcpServers.blender` |
| cline | `cline_mcp_settings.json` | `mcpServers.blender` |
| roo | `.roo/mcp.json` | `mcpServers.blender` |
| continue | `~/.continue/config.json` | `mcpServers.blender` |
| cursor | `~/.cursor/mcp.json`, `.cursor/mcp.json` | `mcpServers.blender` |
| windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers.blender` |
| gemini | `~/.gemini/settings.json` | `mcpServers.blender` |
| codex | `~/.codex/config.toml` | `[mcp_servers.blender]` |
| freebuff / any stdio | `./mcp.json` | `mcpServers.blender` |

Env vars respected: `BLENDER_MCP_HOST/PORT/TIMEOUT/ROOT/ALLOWED_ROOTS/AUTOLAUNCH`, `BLENDER_EXE`, `BLENDER_MCP_REPO`.
