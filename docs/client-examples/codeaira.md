# Codeaira

File: `codeaira/mcpservers/config.yaml`.

Generate the server block:

```bash
hyperbox config --format yaml
```

## Windows

```yaml
name: My Config
version: 1.0.0
schema: v1

mcpServers:
  - name: hyperbox
    command: "C:\\Users\\You\\.local\\bin\\hyperbox.exe"
    args: []
    env:
      PATH: "C:\\Users\\You\\.local\\bin;C:\\Program Files\\RedHat\\Podman;C:\\Windows\\System32;C:\\Windows"
    connectionTimeout: 30000
```

The paths are double-quoted because YAML's double-quoted style uses the
same escape rules as JSON, so a lone backslash would be an escape
character there too. Single-quoted or unquoted scalars follow different
rules; `hyperbox config` emits the double-quoted form deliberately.

## About `connectionTimeout`

30000 ms is 30 seconds, and a first-use image pull is several gigabytes —
it will not finish in that window whatever the value is set to. Pull once
during setup instead:

```bash
hyperbox doctor --pull
```

After that, creating a sandbox takes seconds and the timeout is not a
factor. The server reports progress throughout creation regardless, so a
client that treats the timeout as idle-time rather than total-time will
not cut it off.
