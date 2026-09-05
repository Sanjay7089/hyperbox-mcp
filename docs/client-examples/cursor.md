# Cursor / VS Code

File: `.vscode/mcp.json` in whichever project you want the sandbox
available in. Cursor uses the key `servers`, not `mcpServers`.

Generate it:

```bash
hyperbox config --format cursor > .vscode/mcp.json
```

## Windows

```json
{
  "servers": {
    "hyperbox": {
      "command": "C:\\Users\\You\\.local\\bin\\hyperbox.exe",
      "args": [],
      "env": {
        "PATH": "C:\\Users\\You\\.local\\bin;C:\\Program Files\\RedHat\\Podman;C:\\Windows\\System32;C:\\Windows"
      }
    }
  }
}
```

Every backslash is doubled because JSON treats a single one as an escape
character. `hyperbox config` does this for you; hand-editing is where it
goes wrong.

## macOS / Linux

```json
{
  "servers": {
    "hyperbox": {
      "command": "/Users/you/.local/bin/hyperbox",
      "args": [],
      "env": { "PATH": "/Users/you/.local/bin:/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

## Notes

- `args` is empty. Anything of the form
  `["run", "--project", "…", "hyperbox"]` is launching through a
  checkout — see [README.md](README.md).
- `PATH` must include your container CLI's directory. Clients launch
  servers with a trimmed environment, so the engine is often invisible
  otherwise.
- Restart the client after editing; MCP configs are read at launch.
