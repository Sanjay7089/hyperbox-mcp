---
description: Verify the sandbox lifecycle (create/run/destroy) works against a real container. Use after any change to runtime.py, llm_sandbox_runtime.py, or server.py, or when asked to verify, test, or check that the sandbox works.
---

## Run the acceptance test

!`uv run python tests/verify.py`

## Instructions

The output above is the real result of driving actual containers — not
something to re-derive by reading source. Report plainly which cases
passed and which failed. For any failure, quote the actual
stdout/stderr/exit_code before proposing a fix.

**Triage the layer before editing code.** If `create_sandbox` itself
fails, the cause is very often the environment, not `runtime.py`: is
Docker/Podman actually running here? Is the socket reachable
(DOCKER_HOST)? Is the base image pullable from this network? Rule those
out before assuming the implementation is broken — a failing test can be
the implementation, the test assumption, the environment, or the
fixture, and picking the wrong layer wastes the most time.

Do not mark the lifecycle as working because the code "looks right"
against the API. This skill exists so nobody has to take that on faith.
