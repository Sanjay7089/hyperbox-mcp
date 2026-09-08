# Security policy

## Reporting a vulnerability

Please [open a GitHub security advisory](https://github.com/Sanjay7089/hyperbox-mcp/security/advisories/new)
rather than a public issue.

Include the engine and version (`hyperbox doctor` prints both), your OS, and
the smallest thing that reproduces it. You will get an acknowledgement, and
a fix or an explanation of why the behaviour is intended.

## Supported versions

The latest released version on PyPI. HyperBox is pre-1.0 and fixes land in a
new release rather than being backported.

## What counts

HyperBox's claim is narrow and stated plainly in
[docs/security.md](docs/security.md): a sandbox is a container with
server-set limits that are **read back off the running container**, and with
its network **detached and proven unreachable** before any submitted code
runs. Anything that breaks one of those is a vulnerability. For example:

- A sandbox handed back that is not actually limited, or whose network is
  reachable.
- Submitted code reaching the host filesystem, the engine socket, or another
  sandbox.
- A tool reporting success for something it did not do — a destroy that left
  the container running, a timeout that stopped nothing.
- Caller input escaping validation: a `sandbox_id` that traverses paths, a
  package name that becomes a pip flag.

## What does not

These are documented limitations, not defects. They are covered in full
under [*What this does NOT protect against*](docs/security.md#what-this-does-not-protect-against):

- **Kernel-level container escapes.** Containers share your host's kernel.
  HyperBox provides no VM or microVM boundary and does not claim to; if you
  need one for genuinely adversarial code, use a VM sandbox.
- **Code running as root inside the container.** Deliberate, and explained
  in the security model — running as a non-root user breaks the execution
  backend outright.
- **Unvetted packages from the public index.** `packages` is trusted input.
- **Anything you deliberately put inside the sandbox**, credentials
  included.

Please report those upstream to the container engine or the package index
where they belong.
