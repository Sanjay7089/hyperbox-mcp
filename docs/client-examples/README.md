# Client configuration examples

**Generate these rather than copying them.** The paths below are examples
and will not match your machine:

```bash
hyperbox config                  # Claude Desktop and most clients
hyperbox config --format cursor  # Cursor / VS Code
hyperbox config --format yaml    # Codeaira
```

The command fills in its own absolute path and a `PATH` containing your
container CLI, and escapes both correctly for the target format. That
matters most on Windows, where a path routinely contains a space and every
backslash must be escaped — a mistake there shows up as "no tools
appeared", with no error anywhere.

Guidance is printed to stderr and the config to stdout, so this writes a
clean file:

```bash
hyperbox config --format cursor > .vscode/mcp.json
```

## No checkout required

Install once, and the repository is no longer involved:

```bash
uv tool install "git+https://github.com/Sanjay7089/hyperbox-mcp@v0.1.1"
```

A config that launches the executable directly keeps working if the clone
is moved or deleted. If you are still launching through a checkout —

```json
"command": "…\\uv.exe",
"args": ["run", "--project", "…\\hyperbox-mcp", "hyperbox"]
```

— that works, but it ties every client to that folder and re-resolves the
project environment on each launch. `hyperbox config` warns when it
detects it is being run that way.

## Before the first sandbox

```bash
hyperbox doctor --pull
```

The language image is several gigabytes. Pulling it once during setup
means the download never happens inside a client request, which no client
will wait for — Codeaira defaults to a 30-second timeout, and others are
not much longer.

## Examples

- [cursor.md](cursor.md) — Cursor / VS Code, `.vscode/mcp.json`
- [codeaira.md](codeaira.md) — Codeaira, `mcpservers/config.yaml`
- [claude-desktop.md](claude-desktop.md) — Claude Desktop
