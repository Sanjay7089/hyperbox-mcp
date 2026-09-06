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

Please open a GitHub security advisory rather than a public issue.
