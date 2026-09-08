# Contributing

Thanks for taking a look.

## Getting set up

```bash
git clone https://github.com/Sanjay7089/hyperbox-mcp
cd hyperbox-mcp
uv sync
uv run hyperbox doctor
```

You need Python 3.11+ and either Docker or Podman running.

## Running the tests

```bash
uv run python tests/verify_platform.py     # host-side only, seconds
uv run python tests/run_all.py docker      # full suite, 5-15 minutes
uv run python tests/run_all.py podman
```

**There are no mocks on the sandbox path, on purpose.** Mocking a
container engine proves only that the mock works. Acceptance is defined
against a real container, which is why the suite is slow. Please keep it
that way.

The full suite must pass on **both** engines before a change lands. It
also asserts that no containers were left behind.

## Testing an unreleased branch

Automated suites cannot cover the client integration, and the machine you
develop on is usually not the one that breaks. To exercise a branch on
another machine without installing from PyPI:

```bash
git clone -b <branch> https://github.com/Sanjay7089/hyperbox-mcp hyperbox-branch
cd hyperbox-branch
python -m venv .venv && . .venv/bin/activate
pip install -e .
hyperbox --version                 # confirm it is the branch, not a release
python tests/verify_platform.py    # host side, seconds
python tests/run_all.py docker     # or: podman
```

**Check which `hyperbox` you are actually running.** If a release is
already installed, `hyperbox` resolves through PATH to *that* build — so
generate the client config with `--local`, which pins the executable in the
environment you ran it from:

```bash
hyperbox config --local --format json          # Claude Desktop
hyperbox config --local --format antigravity   # Antigravity
```

Without it, the client launches the installed release, your branch never
runs, and nothing about the config looks wrong.

### On Windows, in Git Bash

Git Bash works, with three differences that will otherwise waste an hour.

```bash
# 1. Remove any installed release first: PATH wins over a checkout.
pip uninstall -y hyperbox-mcp

git clone -b <branch> https://github.com/Sanjay7089/hyperbox-mcp hyperbox-branch
cd hyperbox-branch

# 2. The venv lives in Scripts/, not bin/, and activate has no extension.
py -3.11 -m venv .venv
source .venv/Scripts/activate

pip install -e .
which hyperbox        # must be .../hyperbox-branch/.venv/Scripts/hyperbox
hyperbox --version
```

**Disable MSYS path conversion for container commands.** Git Bash rewrites
arguments that look like Unix paths, so `podman exec ... /bin/sh` becomes
`C:/Program Files/Git/usr/bin/sh` inside the container and fails with a
confusing "no such file". Prefix any engine command that carries an
absolute path:

```bash
MSYS_NO_PATHCONV=1 podman run -d --rm --name probe alpine sleep 300
MSYS_NO_PATHCONV=1 podman exec probe /bin/sh -c 'echo hello'
```

The Python suites are unaffected — they never pass paths through the shell.

**Podman is the usual Windows engine, and it is the harder one.** podman-py
has no named-pipe transport at all, so HyperBox reaches Podman on Windows
through the Docker-compatible API it already serves. Confirm a machine is
actually up before blaming HyperBox:

```bash
podman machine list      # a machine must be "Currently running"
podman machine start     # first time: podman machine init
podman version           # does the CLI itself reach it?
hyperbox doctor          # names the pipe actually in use
```

Then run the suites against podman, not docker:

```bash
python tests/verify_platform.py
python tests/run_all.py podman
```

### What needs Administrator on Windows, and what does not

Measured on a clean Windows Server 2025 box with a non-admin account, not
assumed.

**No administrator needed:**

- `pip install hyperbox-mcp` into a virtualenv, straight from PyPI.
- Running everything: `hyperbox`, `hyperbox doctor`, `hyperbox config`, the
  MCP server itself, and the test suites. HyperBox writes only to
  `%USERPROFILE%\.hyperbox`.

**Administrator needed, once, to prepare the machine:**

- Installing Python, Git and Podman *machine-wide*. A per-user Python is
  enough if you never need other accounts to run it.
- Enabling the `Microsoft-Windows-Subsystem-Linux` and
  `VirtualMachinePlatform` Windows features, which Podman needs and which
  require a reboot.

So a reviewer can install and exercise HyperBox as an ordinary user; only
preparing the host needs elevation.

### Two traps when automating a Windows box

Both cost real time to diagnose, and neither is obvious.

**WSL will not run as `NT AUTHORITY\SYSTEM`.** It fails with
`Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`. Remote-execution tooling
(AWS SSM `send-command`, many CI agents, services) runs as SYSTEM by
default, so Podman can never start there however the machine is
configured. Run container work as a real user account — a scheduled task
with `/RU <user>` is enough.

**Per-user MSIs installed as SYSTEM land in SYSTEM's profile.** Installing
Podman over SSM put it at
`C:\Windows\System32\config\systemprofile\AppData\Local\Programs\Podman\`,
where no interactive user can see it, while the uninstall registry happily
reported it installed. Pass `ALLUSERS=1 MSIINSTALLPERUSER=0` to msiexec and
confirm the binary is where you expect.

### A Windows box with no container engine

Still worth having, and quick to set up. It proves the host-portability
suite, the named-pipe transport (`tests/verify_named_pipe.py`, which drives a
stub server and needs no engine), client-config generation, and the packaging
and privilege questions above.

The container suites need Podman, which needs WSL2. An ordinary cloud
instance runs it — see [Standing up a Windows test box on
EC2](#standing-up-a-windows-test-box-on-ec2). If WSL says *"virtualisation is
not enabled on this machine"*, that is the missing second reboot far more
often than it is the instance type.

### The client checks the suites cannot do

Drive the loop by hand in a real client. In rough order of what they catch:

1. **Open a second client window and use both at once.** Multi-client
   problems reproduce nowhere else.
2. **Restart the client, then destroy a sandbox created before the
   restart.** Cross-process ownership is the property most likely to break
   silently.
3. create → code that succeeds → code that fails → read the real traceback
   → destroy.
4. `hyperbox build` an environment while the server is running, then use it
   from the agent without restarting.
5. Nothing left behind:
   `podman ps -a --filter label=hyperbox-mcp.managed=true`

Quit every other HyperBox server first. They share the machine's engines,
the `hyperbox-mcp.managed` label and the registry, so a second one creates
containers your run did not and garbage-collects on its own schedule.
`python tests/run_all.py` warns when it finds them.

## Building HyperBox on macOS

```bash
git clone https://github.com/Sanjay7089/hyperbox-mcp && cd hyperbox-mcp
uv sync                                   # or: python -m venv .venv && pip install -e .
uv run hyperbox doctor                    # is this machine able to run sandboxes?
```

You need Python 3.11+ and Docker or Podman running. Both work; `auto`
prefers Docker only as a stable default.

**Podman on macOS needs its machine started**, and HyperBox talks to its
unix socket rather than the TCP port the machine also publishes — over the
forward, exec returns correct exit codes and zero bytes of output:

```bash
brew install podman        # or the .pkg, which installs to /opt/podman/bin
podman machine init && podman machine start
```

`podman` need not be on PATH: HyperBox finds the socket itself and reports
what it used in `hyperbox doctor`.

### Running it

```bash
uv run python tests/verify_platform.py         # host side, seconds, no engine
uv run python tests/run_all.py docker          # full suite, 4-5 minutes
uv run python tests/run_all.py podman          # 10-16 minutes; podman is slower
HYPERBOX_RUNTIME=native uv run python tests/run_all.py docker
```

`HYPERBOX_RUNTIME` selects the execution backend — `llm-sandbox` or
`native`. Both must pass before a change lands.

### Building a release artifact

```bash
rm -rf dist build
uv build                                       # wheel + sdist into dist/
uv tool run --from twine twine check dist/*
uv tool run --from twine twine upload --repository testpypi dist/*
```

Install what you built into a clean environment before publishing for real
— an editable install hides packaging mistakes, because it never exercises
the wheel:

```bash
uv tool install --force ./dist/hyperbox_mcp-*.whl
hyperbox doctor
```

## Standing up a Windows test box on EC2

Recorded because it took several wrong turns, two of which looked like
hardware limits and were not.

**It works.** An ordinary `m7i-flex.large` running Windows Server 2025 runs
WSL2, Podman and the full container suite. Nested virtualisation is
available; the failures below are what it looks like when it is not yet
*enabled*.

### 1. The instance

Any general-purpose type with **at least 80 GB** of root volume. Windows
Server 2025 alone occupies about 25 GB; a Podman VM plus the five language
images needs roughly 20 GB more. A 30 GB disk fills silently and the first
symptom is SSM refusing every command with *"There is not enough space on
the disk"* — at which point the machine cannot be fixed remotely, because
the tool you would fix it with needs somewhere to write.

Attach an instance profile granting `AmazonSSMManagedInstanceCore`, then:

```bash
aws ssm describe-instance-information --region <region> \
  --filters "Key=InstanceIds,Values=<id>" --query 'InstanceInformationList[].PingStatus'
```

### 2. Run commands as a real user, not as SYSTEM

`aws ssm send-command` executes as `NT AUTHORITY\SYSTEM`, and **WSL refuses
to run as SYSTEM** — `Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`. No amount of
configuration changes that, so Podman can never start over plain SSM.

Create a real account and drive it through a scheduled task:

```powershell
New-LocalUser -Name hbtest -Password (ConvertTo-SecureString "<pw>" -AsPlainText -Force) -PasswordNeverExpires
Add-LocalGroupMember -Group Administrators -Member hbtest
# grant SeBatchLogonRight via secedit, then:
schtasks /create /tn hbjob /tr "cmd.exe /c powershell -File C:\job.ps1 > C:\out.txt 2>&1" `
         /sc once /st 00:00 /ru hbtest /rp "<pw>" /rl HIGHEST /f
schtasks /run /tn hbjob
```

### 3. Install the toolchain machine-wide

```powershell
# Git (brings Git Bash), Python 3.11, both silent and for all users
Start-Process git-installer.exe -ArgumentList "/VERYSILENT","/NORESTART" -Wait
Start-Process python-installer.exe -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1" -Wait

# Podman: ALLUSERS=1 matters. A per-user MSI run as SYSTEM installs into
# C:\Windows\System32\config\systemprofile\..., where no interactive user
# can see it, while the uninstall registry cheerfully reports it installed.
msiexec /i podman-installer-windows-amd64.msi /quiet /norestart ALLUSERS=1 MSIINSTALLPERUSER=0
```

### 4. WSL2 — and the reboot that is easy to miss

```powershell
dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
Restart-Computer -Force
# then the standalone MSI: `wsl --install` fetches from the Store, which
# SYSTEM cannot reach
msiexec /i wsl.<version>.x64.msi /quiet /norestart
Restart-Computer -Force        # <-- REQUIRED, and the one that gets skipped
```

**Two reboots, not one.** The first activates the Windows features; the
second activates the kernel driver the WSL MSI installs. Between them, WSL
reports *"WSL2 is unable to start since virtualization is not enabled on
this machine"* — which reads exactly like an unsupported instance type and
is not. Verify with a throwaway distro before blaming the hardware:

```powershell
wsl --install -d Ubuntu --no-launch
wsl -d Ubuntu -- echo WORKS
wsl --unregister Ubuntu
```

### 5. Podman, as `hbtest`

```powershell
podman machine init --now
podman machine list          # must say "Currently running"
podman run --rm alpine echo WORKS
```

It publishes `npipe:////./pipe/docker_engine`, which is what HyperBox's
named-pipe transport dials.

### 6. Run the suites

```bash
git clone -b <branch> https://github.com/Sanjay7089/hyperbox-mcp
cd hyperbox-mcp && py -3.11 -m venv .venv && .venv\Scripts\python -m pip install -e .
.venv\Scripts\python tests\verify_named_pipe.py          # no engine needed
set HYPERBOX_RUNTIME=native
.venv\Scripts\python tests\run_all.py podman
```

Run long jobs detached and poll a log file. SSM's document worker times out
well before a full suite finishes, and it takes the output with it:

```powershell
Start-Process cmd.exe -ArgumentList "/c suite.cmd > C:\suite.log 2>&1"
```

### Costs worth knowing

Stop the instance when idle. Growing an EBS volume is **one-way** — they
can be enlarged but never shrunk — so size the root volume correctly at
launch rather than discovering it full later.

## The rules that hold this together

A few constraints are load-bearing. A change that breaks one of these
needs a good argument, not just a passing test.

1. **The Runtime boundary.** The tools talk only to the `Runtime`
   protocol in `runtime.py`. `llm_sandbox` may be imported *only* in
   `llm_sandbox_runtime.py`. That is what keeps the execution backend
   replaceable.

2. **Limits are server policy, never a caller's choice.** Memory, CPU,
   PIDs, network posture, scratch size and the timeout ceiling live in
   `policy.py`. Do not add a tool parameter that lets a caller raise any
   of them.

3. **Tool descriptions are routing logic.** An MCP client has no
   dispatcher — it decides whether to call a tool from that tool's name,
   description and annotations alone. When you change a tool, say what it
   is for **and when not to use it**, and keep the annotations honest.

4. **An entry in a supported map is a promise.** Adding a language,
   backend or built-in environment says HyperBox can deliver it on
   someone else's machine. Nothing goes in until the full suite passes
   for it against a real container.

5. **A slow operation must keep talking.** MCP clients kill a tool call
   after a period of silence, and creation can pull gigabytes. Report
   progress; silence is indistinguishable from a hang.

6. **A dropped connection is not an outage.** Retry a connection-shaped
   failure once against a fresh client, and never widen that to cover a
   genuinely unreachable engine. The registry design rests on telling
   those apart.

7. **Validate at the boundary, act under the lock.** Validate inputs,
   take the per-sandbox lock, then re-read the record inside it.

## Pull requests

- One concern per PR.
- Say in the description which engines you ran the suite against.
- If you fixed something subtle, put the *why* in a comment next to the
  code. This codebase records the reasoning behind non-obvious decisions
  on purpose.

## Reporting security issues

Please open a GitHub security advisory rather than a public issue. See
[SECURITY.md](SECURITY.md) for what is in scope and what is a documented
limitation.
