# Antigravity

File: `~/.gemini/antigravity/mcp_config.json`. Antigravity uses the key
`mcpServers`, the same shape Claude Desktop uses.

Generate it:

```bash
hyperbox config --format antigravity
```

That prints the block on stdout and the guidance on stderr, so you can
redirect it cleanly — but this file usually already has other servers in
it, so merge rather than overwrite.

## macOS / Linux

```json
{
  "mcpServers": {
    "hyperbox": {
      "command": "/Users/you/.local/bin/hyperbox",
      "args": [],
      "env": { "PATH": "/Users/you/.local/bin:/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

## Windows

```json
{
  "mcpServers": {
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

## Notes

- Restart Antigravity after editing. The file is read at launch, and a
  bad entry fails silently — the symptom is that no HyperBox tools
  appear, with no error anywhere.
- `args` is empty. Anything of the form
  `["run", "--project", "…", "hyperbox"]` is launching through a source
  checkout: it breaks the moment that folder moves, is deleted, or is on
  a different branch. Install the tool instead:

  ```bash
  uv tool install hyperbox-mcp
  ```

- `PATH` must include your container CLI's directory. Clients launch
  servers with a trimmed environment, so Docker or Podman is often
  invisible otherwise.
- Running two entries at once (say `hyperbox` and `hyperbox-beta`) is
  fine — they are separate processes and the registry is safe across
  them — but both reclaim expired sandboxes, so a container created by
  one may be garbage-collected by the other. That is by design; they
  share the same labels.
