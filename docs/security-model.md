# Security model

HyperBox runs code an agent wrote, in a container, instead of on your
machine. This document states exactly what that does and does not buy
you. Read the last section before relying on it for anything adversarial.

## What is enforced

Set by the server. No tool parameter raises any of it, and no request
from a model changes it.

| Control | Value | How it is proved |
|---|---|---|
| Memory | 1 GB, OOM-killed with a legible reason rather than a bare exit 137 | read back off the container after creation, and asserted in the suite |
| CPU | 1 core | same — recorded as `NanoCpus` by Docker, as a quota over a period by Podman |
| Processes | 128 PIDs | same |
| Network | detached before any submitted code runs | zero attached networks asserted structurally, plus a live connection attempt |
| Timeout | 60 s ceiling; `null`, negative, zero, NaN and infinity all rejected | validation suite |
| Output | capped per stream, and marked when truncated | limits suite |
| Code size | 1 MiB per call | validation suite |
| Host filesystem | never mounted | no mount with a host source, asserted structurally |
| Container engine socket | never mounted | asserted structurally, and probed from inside |
| Privilege escalation | `no-new-privileges`; not privileged; no added capabilities | asserted structurally |
| Scratch space | `/work`, a 64 MB tmpfs, discarded with the sandbox | asserted structurally and measured with `df` inside the container |

After a container is created, its real configuration is read back off the
engine and compared to this policy. **A mismatch destroys the container
and fails the call.** This is not a formality — it caught Podman silently
discarding the CPU limit (see below).

## The network exception, stated plainly

A sandbox has no network while your code runs. It gets one for exactly
one purpose: installing packages you declared.

When `run` is called with `libraries`, the server attaches a network,
installs those packages **without running your code**, detaches the
network, and only then executes what you submitted. So submitted code
never executes with network access.

Two limits on what that window can be used for:

- Only plain package names are accepted — `requests`, `pandas==2.2.0`,
  `uvicorn[standard]`. Installer flags, URLs, filesystem paths and VCS
  references are refused. The install command is built by string
  interpolation and executed as a shlex-split argv, so shell
  metacharacters are inert, but `--index-url http://elsewhere/` would
  otherwise be a live pip flag taking effect during the one moment the
  sandbox can reach the network.
- At most 25 packages per call.

If a package install is itself hostile, it runs inside the sandbox with
the same limits as everything else.

## What was tried and rejected

Both of these were measured against real containers and reverted, rather
than assumed:

**Running as a non-root user.** The execution backend provisions a
virtualenv under `/sandbox` during environment setup, which needs root in
these images. As uid 1000 the venv is never created and every subsequent
exec fails with 127 — the sandbox is unusable, not merely less
privileged. Code therefore runs as root *inside the container*. That root
is confined by the container boundary, `no-new-privileges`, and the
limits above; it is not root on your machine. A future image built for
non-root execution would let this change, and the suite records the
observed user on every run so this document cannot quietly drift from
reality.

**Dropping all capabilities (`cap_drop: ALL`).** Breaks the backend even
as root: without `CAP_DAC_OVERRIDE` it cannot read the file it just
copied into `/sandbox`, and every run fails with a permission error.

**A size-limited tmpfs over `/tmp`.** Not portable. Podman's crun rejects
it with "No space left on device" whatever options are passed, and an
unsized tmpfs defaults to half of RAM, which is not a limit. `/work` is
the bounded scratch directory; `/tmp` is writable but not separately
capped, though it is still discarded with the container.

## Engine differences that mattered

Docker and Podman are not interchangeable at the client level, and the
differences were failure-shaped rather than cosmetic:

- **`podman-py` silently discards `nano_cpus`.** It appears in that
  library's explicit "Ignore these keywords" list. Passing it produced a
  container reporting `CpuQuota=0` — no CPU limit at all — while the
  server would have gone on advertising a 1-core ceiling to the agent.
  Podman is now configured with `cpu_period`/`cpu_quota`, and the
  post-create check accepts either spelling as proof and neither's
  absence as failure.
- **`podman-py` rejects the `tmpfs` keyword** and wants tmpfs entries
  inside `mounts`.
- **Privilege flags differ**: `security_opt` versus `no_new_privileges`.

The general lesson is the reason the post-create verification exists: an
engine that accepts a configuration and applies none of it is worse than
one that refuses it, because the server would keep describing the sandbox
as limited.

## Failure is never reported as success

The registry exists to stop containers being orphaned, so the server
distinguishes two things that a single `except` would merge:

- **The engine answered and the container is not there** — that is a
  successful cleanup.
- **The engine could not be reached** — that is an error. The sandbox
  stays on file and `destroy_sandbox` says so, because a container that
  cannot be reached has not been proven gone.

This is tested against a genuinely dead socket, not a mock.

## What this does NOT protect against

Be clear-eyed about this. HyperBox is developer containment, not an
isolation guarantee.

- **The container shares your host's kernel.** A kernel exploit or a
  container escape reaches your machine. There is no gVisor, no
  Firecracker, no VM boundary of our own. (On macOS and Windows, Docker
  Desktop and Podman machines happen to run containers inside a Linux VM,
  which adds a boundary — but that is a property of your setup, not
  something HyperBox provides or can promise.)
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
- **The brief network window during a dependency install** is a real
  window. It is narrow and constrained to named packages, but it exists.

If you need a hard isolation boundary for genuinely adversarial code, you
need a VM or a microVM sandbox, not a local container.
