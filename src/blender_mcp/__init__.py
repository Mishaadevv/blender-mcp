"""MCP server for Blender 4.5 LTS."""

from .client import BlenderBridge, BlenderError, find_blender_executable

__all__ = ["BlenderBridge", "BlenderError", "find_blender_executable", "main"]
__version__ = "1.0.0"


def main() -> None:
    from .server import main as _main

    _main()
