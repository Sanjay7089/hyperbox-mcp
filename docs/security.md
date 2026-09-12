# Security model

HyperBox is **developer containment**: it stops an agent's code from
touching your machine by accident. It is not an isolation guarantee
against a determined attacker. This page says exactly where that line
sits.

## What is enforced

Every control below is applied by the server, cannot be raised by a
caller, and is proven by the acceptance suite against a real container.

| Control | Value | How it is proven |
|---|---|---|
| Memory | 1 GB, OOM-killed with a legible reason | read back off the container |
| CPU | 1 core | read back off the container |
| Processes | 128 PIDs | read back; a fork bomb halts at 126 |
| Network | detached before any submitted code runs | zero attached networks, plus a TCP connection and a DNS lookup that must both fail, from inside |
| Timeout | 60 s ceiling; null, negative, zero, NaN and infinity refused | validation suite |
| Output | capped per stream, marked when truncated | limits suite |
| Code size | 1 MiB per call | validation suite |
| Host filesystem | never mounted | no mount with a host source |
| Engine socket | never mounted | structural check, plus probed from inside |
| Privilege escalation | `no-new-privileges`; not privileged; no added capabilities | structural check |
| Scratch space | `/work`, 64 MB tmpfs, discarded with the sandbox | structural check, plus `df` inside |

**Limits are verified, not assumed.** After a container is created, its
configuration is read back off the engine and compared to policy. A
mismatch destroys the container and raises. This exists because an engine
that accepts a configuration and silently applies none of it would
otherwise hand back a sandbox that gets *described* to an agent as
limited while being nothing of the sort.

## The network is severed before your code runs, and proven severed

**Unless you deliberately built an environment that keeps it.** That is
the one exception, it is described in full below, and nothing an agent
can say brings it about — see *Environments that keep their network*.

For every ordinary sandbox: submitted code never runs with network
access. There is exactly one moment a sandbox can reach the internet, and
it is over before the sandbox is handed to you:

1. The container starts with a network attached.
2. Packages you declared in `create_sandbox(packages=[...])` are
   installed. **No submitted code has run at this point** — only the
   installer.
3. Every network is detached.
4. The seal is **verified from inside the container**: a TCP connection to
   a public address and a DNS lookup must both fail. If either succeeds,
   the container is destroyed and creation fails with `NETWORK_LEAK`.
5. Only then do you get a sandbox id.

Both probes matter. Detaching a network does not remove the resolver the
container inherited from it — `/etc/resolv.conf` survives — so name
resolution can keep working after every route is gone, and a TCP-only
check would call that sealed.

Sealing **fails closed** twice over: if the network cannot be detached, no
sandbox is returned; if it is detached and the sandbox can still reach
out, no sandbox is returned either.

Package names are matched against a narrow subset of PEP 508 — no URLs,
paths, VCS references or flags. `--index-url http://…` is a real pip flag,
and it would otherwise take effect during the one window where the sandbox
has network access.

### `run(libraries=...)` is deprecated

It still works, and it still installs in a separate step with the caller's
code held back. But it re-opens the network **on a sandbox that was
already sealed**, which is precisely the window create-time provisioning
removes. Declare packages at create time instead. It will be refused in a
future release.

## Custom environments

`create_sandbox(environment=...)` starts from an image you built with
`hyperbox build`. This changes the base image and nothing else — every
limit is still applied and still verified against the created container.

**There is no MCP tool that builds an environment.** A build runs whatever
the Dockerfile says: arbitrary commands, as root, with network, under none
of a sandbox's caps. Giving an agent that capability would hand it,
through the build path, exactly the unconstrained host execution the
sandbox exists to deny.

So the trust boundary for an environment is whoever wrote its Dockerfile.
HyperBox validates the environment's *name*; it does not and cannot
inspect what the image contains.

## Code runs as root inside the container

Deliberately, and documented rather than hidden. Two standard hardening
measures were tried against real containers and reverted:

- **A non-root user** makes the container unusable. The execution backend
  provisions a virtualenv under `/sandbox` during setup, which needs root
  in these images; as uid 1000 every subsequent exec fails with 127.
- **`cap_drop: ALL`** breaks it even as root: without `CAP_DAC_OVERRIDE`
  the backend cannot read the file it just copied into `/sandbox`.

That root is confined by the container boundary, `no-new-privileges`, and
the resource limits above. It is not root on your machine. An image built
for non-root execution would let this change, and the suite records the
observed user on every run so this page cannot quietly drift.

## What this does NOT protect against

- **The container shares your host's kernel.** A kernel exploit or
  container escape reaches your machine. There is no gVisor, no
  Firecracker, no VM boundary of HyperBox's own. On macOS and Windows,
  Docker Desktop and Podman machines happen to run containers inside a
  Linux VM, which adds a boundary — but that is a property of your setup,
  not something HyperBox provides.
- **It is not a multi-tenant boundary.** Do not use it to run untrusted
  third-party code as a service, or to separate one customer's code from
  another's.
- **Declared dependencies come from the public index** and are not
  vetted, pinned by hash, or audited.
- **Anything you deliberately put inside the sandbox is inside it.** Do
  not paste credentials into code you send to `run`.
- **Denial of service against your own machine is only partly bounded.**
  Memory, CPU and PIDs are capped per sandbox, but many sandboxes, or a
  full `/tmp`, can still consume host resources.
- **A window exists between container start and network sealing.** It is
  when your declared packages are installed. Nothing you submitted runs in
  it, and the sandbox is not handed back until the network is detached and
  verified unreachable — but the window is real and is how a malicious
  package would reach the network, so treat `packages` as the trusted
  input it is.
- **`run(libraries=...)`, if you use it, re-opens that window** on an
  already-sealed sandbox. It is deprecated for exactly this reason.

If you need a hard isolation boundary for genuinely adversarial code, you
want a VM or microVM sandbox, not a local container.

## Reporting a vulnerability

Open a GitHub security advisory on the repository rather than a public
issue.
