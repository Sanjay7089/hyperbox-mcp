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
| Network | detached before any submitted code runs | zero attached networks, plus a live connection attempt |
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

## The network exception

Submitted code never runs with network access. Declared dependencies are
the one exception, and they are handled in a separate step:

1. The container starts and is immediately detached from every network,
   before any caller code runs.
2. If a `run` call declares `libraries`, the network is reattached, a
   **no-op program** is executed with the package list, and the network
   is detached again in a `finally` block.
3. Only then does your code run.

Library names are matched against a narrow subset of PEP 508 — no URLs,
paths, VCS references or flags. `--index-url http://…` is a real pip
flag, and it would otherwise take effect during the one window where the
sandbox has network access.

Sealing **fails closed**: if the network cannot be detached, no sandbox is
returned.

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
- **The brief network window during a dependency install** is real. It is
  narrow and constrained to named packages, but it exists.
- **A brief window exists between container start and network sealing.**
  Only the backend's own environment setup runs in it; nothing an agent
  submitted is ever executed unsealed.

If you need a hard isolation boundary for genuinely adversarial code, you
want a VM or microVM sandbox, not a local container.

## Reporting a vulnerability

Open a GitHub security advisory on the repository rather than a public
issue.
