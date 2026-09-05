# Claude Desktop

File:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Generate it:

```bash
hyperbox config
```

```json
{
  "mcpServers": {
    "hyperbox": {
      "command": "/Users/you/.local/bin/hyperbox",
      "args": [],
      "env": { "PATH": "/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

Merge the `hyperbox` entry into any existing `mcpServers` object rather
than replacing the file.

**Quit with ⌘Q, not by closing the window.** Claude Desktop reads its
config only at launch, and a closed window leaves the process running —
the most common reason a newly added server does not appear.

If it still does not appear, check
`~/Library/Logs/Claude/mcp-server-hyperbox.log`.
