# Decisions

The record of what was decided and why, for a project whose central claim is
that it never asserts something it has not verified. The blueprint
(`HyperBox — Execution Blueprint`) holds the high-level amendment table; this
file holds the reasoning underneath it.

Append-only, newest at the bottom. One entry per decision, written at the moment
it is made rather than reconstructed afterwards — a decision log assembled at the
end of a release is a summary, and summaries lose exactly the close calls that
make a log worth keeping.

## Format

```markdown
## YYYY-MM-DD — <the decision, stated as an outcome>

**Context.** What forced the choice.
**Options.** What was actually on the table.
**Decided.** The choice, in one sentence.
**Because.** The reasoning that settled it.
**Costs.** What this gives up, stated plainly.
**Revisit when.** The condition that would reopen it — or "not expected".
```

`Costs` and `Revisit when` are the load-bearing fields. A log of decisions with
no recorded cost reads, six months later, as a list of obvious choices, and
nobody can tell which ones were close.

---

## Standing notes

Not decisions — facts established by investigation that would otherwise be
re-derived, at cost, by whoever looks next.

**The unsealed-container window is three exclusions conspiring, not one bug.**
`server.create_sandbox` reserves with the default 1800s TTL. A `creating` row is
(1) excluded from `expired_records()`, which filters `state = 'ready'`;
(2) invisible to `stale_reservations()` until `expires_at <= now`; and (3) present
in `known_ids()`, which makes `collect_orphans` skip its container. Each
exclusion is individually correct and documented. Together they shield a started,
root, network-attached, unsealed container from both GC directions for the whole
TTL. The `except BaseException: remove_container` guard in
`native_runtime.create` covers a raise but not SIGKILL, which is what MCP clients
do routinely. Anyone shortening the TTL should understand it is bounding the
window, not closing it — only a check keyed on the container itself closes it.

**The three-phase split needs server-authored inspection tools to be coherent.**
Its motivation is "sync the repo, look at it, then decide what to install". To
look at the repo *inside* the container something must execute there, and during
the unsealed window that container has a network. So the split is only safe once
`read_file`/`list_dir`/`search` exist as server-authored argv — with those, the
agent inspects without agent-written code ever running unsealed. Without them the
choice is between executing agent code in a networked container (prohibited) and
an inspection capability the agent does not have (useless).

---

## 2026-09-12 — This log is tracked in git; the rest of `internal/` stays local

**Context.** `.gitignore:18` excludes `internal/` entirely, with a recorded
reason: "the public repo documents what HyperBox does, not the story of building
it." A decision record kept only in one working tree is lost on a fresh clone,
by anyone but its author, and on the first machine change.

**Options.** Leave it ignored and accept it is local-only; track this one file;
move it to a tracked location like `docs/`; keep it outside the repo entirely.

**Decided.** Track `internal/decisions.md`; everything else in `internal/`
remains ignored.

**Because.** The stated reason for ignoring `internal/` is that working notes,
reverted experiments and talk material are not product material — which is true
of `engineering-log.md` and is not true of this file. A decision record is the
part of the story that a contributor needs in order to not re-litigate a settled
question. `docs/` was rejected because it is user-facing and this is
project-facing; outside the repo was rejected because a record that does not
travel with the code it describes drifts from it.

Mechanically this needs `internal/` to become `internal/*` — git cannot
re-include a file whose parent *directory* is excluded, so the negation only
works once the pattern excludes the directory's contents instead.

**Costs.** Candid reasoning about tradeoffs becomes repository content, and on
the next push, public. Entries must be written knowing that. This is a real cost
and the reason the option was not obvious.

**Revisit when.** An entry needs to say something that should not be public. At
that point the answer is to split the file, not to untrack it.

## 2026-09-12 — Blueprint item 4.2 (three-phase split) moves to v0.5.0

**Context.** The blueprint schedules the `create → provision → seal` split for
0.4.0, reasoning it is a "free break now" while nothing depends on the surface.

**Options.** Build it in 0.4.0 as written; build it with `create_sandbox` kept as
a one-shot and the long path opt-in; defer to 0.5.0 behind the filesystem tools.

**Decided.** Defer to v0.5.0, sequenced after the filesystem/exec tools.

**Because.** See the standing note above — in 0.4.0 the split buys a persistent
unsealed container in exchange for a capability nothing can use yet, because the
tools that would make inspection safe do not exist until 0.5. It also converts
the product's strongest claim from a guarantee readable in one function
("`create` returns a sealed sandbox or nothing") into a policy spread across four
clocks, three sweeps, a label and a migration.

**Costs.** The "free break" argument weakens slightly: 0.5 has more surface to
break than 0.4 does, and if 0.4.0 gains real users first, the break stops being
free. Accepted because 0.5 is still pre-adoption by any realistic schedule.

**Revisit when.** 0.5 slips far enough that 0.4.0 accumulates users who script
against `create_sandbox`.

## 2026-09-12 — v0.4.0 gains automated coverage for its own headline features

**Context.** `sync_from` (né `sync_in_dir`), `run(background=True)` /
`get_process_logs`, and `hyperbox ps|rm|pull|init` are the three features 0.4.0
exists to ship, and appear in no test file. `get_process_logs` occurs in the
whole test tree only as a string inside verify.py's tool-name assertion.

**Options.** Ship on the manual Gate 3 client checklist; automate the background
path only; automate all three.

**Decided.** Automate all three, in `verify.py`, `verify_security.py` and
`verify_cli.py` respectively.

**Because.** Immutable rule 9 — "an entry in a supported map is a promise;
nothing ships until the full suite passes for it." The `hyperbox init` TTY gate
is the specific control that stops an agent widening its own sync boundary, and
a control nothing tests is a hypothesis.

**Costs.** Delays the ship by the length of three new suites.

**Revisit when.** Not expected.

## 2026-09-12 — The unsealed window is bounded in 0.4.0 by a short creating-TTL

**Context.** The window described in the standing note above, present in shipped
0.3.0.

**Options.** Leave it and fix it with the 0.5 split machinery; add
`policy.CREATING_TTL_SECONDS` and let the existing orphan sweep finish the job;
also add a `hyperbox-mcp.seal-by` container label and a registry-free sweep.

**Decided.** `CREATING_TTL_SECONDS` (600s) now; the `seal-by` label waits for
0.5 Slice C.

**Because.** The TTL is a few lines, needs no schema change, no new state and no
migration, and cuts the exposure from ~30–35 min to ~10–15 min. The label-based
sweep is the only mechanism that survives the owing process being killed, but it
is also the machinery the three-phase split forces — building it once, there,
beats building it twice.

**Costs.** Bounds the window rather than closing it. Between 600s and the next
sweep, an abandoned unsealed container still runs.

**Revisit when.** Slice C lands, at which point the label sweep supersedes this
as the primary bound and the TTL becomes the backstop.

## 2026-09-12 — Two assume-instead-of-verify paths are fixed in 0.4.0

**Context.** `put_tree` and `put_file` discard the HTTP status from
`client._raw`, so a failed archive extraction is invisible at the call site.
`_install` re-seals in a `finally` but never calls `_assert_network_sealed`, so
the deprecated `libraries` path re-seals on faith while `create` proves it.

**Options.** Fix now; fix alongside the 0.5 git-sync work that depends on
`put_tree`.

**Decided.** Fix both in 0.4.0.

**Because.** Both are rule-3 violations ("verify against the thing that
answered") in a release whose entire purpose is to make the project's claims
true. `put_tree` is also the exact path 0.5's repo sync rides on, where a
silently-dropped upload would surface as a mystifying empty workspace.

**Costs.** None identified — these are gaps, not tradeoffs.

**Revisit when.** Not expected.

## 2026-09-12 — Removing `--allow-network` keeps a test, rather than deleting one

**Context.** Cutting the feature meant deleting
`verify_security.py`'s `netopen`/`netsealed` block, which was its only
automated coverage.

**Options.** Delete the block; delete it and assert nothing further; replace it
with a test of the post-removal property.

**Decided.** Replace it: write a manifest carrying the pre-0.4.0
`"network": "bridge"` field by hand, build a sandbox from it, and probe from
inside that it is sealed anyway.

**Because.** Deleting the block would have left the removal verified only by the
absence of code. Old manifests still exist in users' `~/.hyperbox/environments`
and are exactly the input that used to trigger the behaviour — so that input is
what the suite should be pointed at. `_write_manifest` no longer emits the field,
so the test writes it by hand; that is the point, not a workaround.

**Costs.** The test depends on a manifest shape no current code produces, so it
will look strange to anyone who did not live through the removal. The comment
above it carries that context.

**Revisit when.** Pre-0.4.0 manifests are old enough not to plausibly exist.

## 2026-09-12 — `--allow-network` is refused with an explanation, not as an unknown flag

**Context.** After removal, `hyperbox build --allow-network` would fall through
to the generic `unknown option` branch and exit 2.

**Options.** Let it be an unknown option; refuse it with a specific message.

**Decided.** Refuse with a message naming the release, stating that pre-existing
environments still work, and pointing at the issue tracker.

**Because.** It matches how `HYPERBOX_RUNTIME` handles a removed value — refused
with an explanation, not silently ignored. Someone with this flag in a script
needs to know the posture changed; "unknown option" sends them looking for a
typo, which is the confident-wrong-answer failure mode in miniature.

**Costs.** A branch of dead-flag handling to carry. Cheap, and removable once
nobody could plausibly still pass it.

**Revisit when.** 0.5.0, if the flag never returns.

## 2026-09-12 — The SyntaxWarning guard now covers `tests/`, not just `src/`

**Context.** Found while compiling after Phase 1: `tests/verify_cli.py` opened
with an invalid `\p` escape — inside the docstring describing the 0.3.0 `\p`
escape bug that the suite exists to prevent. `verify_platform.py`'s guard only
walked `Path("src")`.

**Options.** Fix the docstring; fix it and widen the guard.

**Decided.** Both — the docstring is now a raw string, and the guard walks
`src/` and `tests/`.

**Because.** A guard that does not cover the guard is half a guard, and this is
the blueprint's own failure pattern ("absence of a warning is only proven if
something looks at stderr") landing on the file written to enforce it.

**Costs.** None.

**Revisit when.** Not expected.

## 2026-09-12 — The reservation-TTL test watches the call site, not the constant

**Context.** Writing coverage for `CREATING_TTL_SECONDS`. The obvious test
compares it to `DEFAULT_TTL_SECONDS` and asserts it is smaller.

**Options.** Compare the constants; force an expiry and assert the sweep; spy on
what `create_sandbox` actually passes to `reserve()`.

**Decided.** Spy on the call site, via an in-process `Client(srv.mcp)`.

**Because.** The defect was never a wrong value in `policy.py` — it was
`server.create_sandbox` letting `reserve()` fall back to its default. Both
weaker tests pass with the defect fully reintroduced; this was confirmed by
reverting the fix and re-running, where the two sweep assertions stayed green and
only the spy went red (`ttl_seconds=1800.0, expected 600.0`). A check that cannot
fail for the reason it exists is decoration.

**Costs.** This one case runs in-process while the rest of `verify_registry.py`
deliberately uses subprocesses, because a spy must share an interpreter with the
thing it watches. The comment above it says so, since the suite's whole premise
is cross-process behaviour.

**Revisit when.** Not expected.

## 2026-09-12 — `_install` proves its reseal, and destroys the sandbox if it cannot

**Context.** `run(libraries=[...])` un-seals a live sandbox, installs, and
re-seals in a `finally`. `create()` probes from inside after sealing; this path
never did.

**Options.** Leave it (the deprecated path is going away); probe and report;
probe and destroy.

**Decided.** Probe and destroy, matching `create()`.

**Because.** The one code path that deliberately opens a hole was the one path
not checking it had closed. Reporting without destroying would leave a live
sandbox that describes itself as sealed and is not — the specific outcome the
whole design is organised against.

The probe sits **outside** the `finally`, deliberately: raising from a `finally`
would swallow whatever exception was already in flight, so an install error
would silently become a leak error, or vice versa.

**Costs.** One extra in-container probe on every `run(libraries=...)` — a path
already deprecated and already slow. Negligible.

**Revisit when.** `libraries` is removed entirely, which takes this with it.

## 2026-09-12 — `hyperbox rm <unknown-id>` exits 0, and the test now says why

**Context.** Writing CLI coverage, the first version asserted that `rm` on an
unknown id exits non-zero. It failed: the command exits 0 with `already gone`.

**Options.** Change the code to exit non-zero; change the test.

**Decided.** Change the test — the behaviour is correct.

**Because.** `rm` inherits `destroy_sandbox`'s documented idempotency: removing
something already gone is the outcome the caller wanted, not an error. A tool
that errors on a repeat destroy forces every caller to special-case success.

**Costs.** None, but the wrong assumption is easy to repeat, so the test carries
a comment saying it was made here and why it is wrong.

**Revisit when.** Not expected.

## 2026-09-12 — `_RestEngine` regains `version()`, and a suite guards it

**Context.** Found while checking doctor output: `hyperbox doctor` reported
`version unknown (AttributeError)` for every engine. `probe()` calls
`engine.version()`, the REST shim that replaced docker-py/podman-py never
defined one, and `probe()` catches the failure because "version is
informational".

**Options.** Leave it (cosmetic); add the method; add the method and test it.

**Decided.** Add it and add a check to `verify_platform.py`.

**Because.** It is not cosmetic in context. The release gate requires the
changelog's `Verified` line to name engine versions, and the tool whose job is
to report them could not — so the number would have been copied off the CLI by
hand, which is exactly the "claim not read back from the thing that answered"
this project exists to avoid. Doctor now reports Docker 29.1.3 and Podman 6.1.1.

This is also a clean instance of a known pattern: an exception handler written
for one reason ("informational") silently absorbing a different failure
(a missing method) for a whole release.

**Costs.** None.

**Revisit when.** Not expected.

## 2026-09-12 — `internal/development.md` yields to `publish.yml` on how to release

**Context.** Two release procedures existed and disagreed: a manual
`twine upload` in `internal/development.md`, and tag-triggered OIDC publishing in
`.github/workflows/publish.yml`.

**Options.** Delete the workflow; delete the manual steps; document both.

**Decided.** The workflow is the procedure; the manual upload step is removed and
the file says so explicitly.

**Because.** Only one can be real, and the workflow is the one wired to
credentials — trusted publishing with no API tokens, which means the manual path
has nothing to authenticate with. Worse, following the manual path first makes
the workflow fail on a duplicate filename, so the stale instructions actively
break the working ones.

**Costs.** Releasing now depends on GitHub Actions being up. Accepted; the
alternative was a procedure that cannot authenticate.

**Revisit when.** Trusted publishing is unavailable and tokens come back.

## 2026-09-12 — A bug in this package is never reported as an engine outage

**Context.** Gate 4 (install the wheel, run `hyperbox doctor`) failed on a
machine with both engines running:

```
FAIL  live sandbox round trip
      Could not reach docker while looking up image python:3.12-slim
      (AttributeError: '_RestEngine' object has no attribute 'images').
      Start the engine: open Docker Desktop...
```

Two defects in one line. The REST shim never grew `images` when the container
SDKs were dropped; and `classify()` turned the resulting `AttributeError` into
`EngineUnavailableError`.

**Options.** Add the missing shim only; add the shim and make `classify` refuse
to relabel programming errors.

**Decided.** Both. `classify` now returns `AttributeError`, `TypeError`,
`NameError` and `ImportError` unchanged.

**Because.** The shim alone fixes this instance; the `classify` guard fixes the
class. Telling a user to restart an engine that is already running is the same
failure the project is organised against — a confident wrong answer — and it is
the exact shape of the `ENGINE_NOT_RUNNING`-on-a-healthy-engine bug 0.4.0
already claims to have fixed, arriving through a different door. Any future
missing shim method would have produced the same misdirection.

**Costs.** `classify`'s contract is now "two truths, unless it is our own bug",
which is a third case in a function whose docstring promises two. Judged worth
it: the alternative is a category of bug that reliably misdirects the user.

**Revisit when.** Not expected.

## 2026-09-12 — The release gates found what seven green suites did not

**Context.** All seven acceptance suites passed on both engines, twice, before
Gate 4 was run. Gate 4 — install the built wheel and run `hyperbox doctor` —
failed immediately on the bug above.

**Decided.** Keep the four gates as separate, mandatory steps; do not let a green
suite stand in for the install gate. `verify_cli.py` now runs the full `doctor`
so this specific gap is closed, but the principle stands.

**Because.** Every doctor case in the suite used `--quick`, which skips the live
round trip — so the one check proving the installed program can make a sandbox
was never run by anything. "It worked in the checkout" is not "it works from the
wheel", and this is the second time that has been true here.

**Costs.** Gate 4 is manual and slow. That is the price of it testing what CI
structurally cannot.

**Revisit when.** Not expected.

## 2026-09-12 — Cursor cannot read `hyperbox://capabilities`; the server is not the cause

**Context.** Running `01-smoke.md` in Cursor, check 2 (read `capabilities`)
returned nothing — no error, just no result. Every tool call in the same run
passed.

**Investigated, not assumed.** Drove the real `hyperbox` subprocess over stdio
with a spec-compliant client (the same `StdioTransport` pattern
`tests/verify_registry.py` uses, not the in-process client `tests/verify.py`
uses), and called `resources/list` then `resources/read` exactly as a real
client would. The server answered correctly: the resource is listed, and reading
it returns well-formed JSON. This rules out the server.

**Conclusion.** MCP defines resources and tools as separate capabilities.
Cursor's tool support is solid (every tool call in the smoke test passed); its
handling of the `resources` capability for autonomous agent reads is the
suspect, not code in this repo. This is exactly the kind of gap the blueprint's
own open-items table anticipated ("can a tool return content the client handles
but the model doesn't ingest?") — same theme, discovered here for the
`resources` primitive specifically, in Cursor specifically, by direct testing
rather than by reading FastMCP's docs.

**Action taken.** Documented as a known client limitation in
`docs/troubleshooting.md`, with the evidence, rather than treated as a bug to
fix. `create_sandbox`'s own tool description already carries every
safety-relevant fact `capabilities` would add (network posture, package
declaration timing, persistence) precisely so a client that never reads
`capabilities` still has what it needs — that design choice is what kept this
from being a real gap in what an agent can safely do.

**Not decided, flagged for later.** Whether HyperBox should ALSO expose a
`get_capabilities` tool as a fallback for clients with partial resource
support. That is tool-surface growth and deserves its own deliberate decision,
not a reflexive fix — especially given `tests/verify.py`'s own argument against
growing the four-tool surface. Left open until more clients are checked (blueprint
open-item table already asks for a real Claude Desktop test too) — if capabilities
turn out unreadable everywhere except the in-process test harness, that changes
the argument; if it's Cursor-specific, it likely doesn't.

**Costs.** None from the documentation change. The open tool-surface question, if
answered yes later, costs exactly what any new tool costs — see verify.py's own
comment on why four was chosen deliberately.

**Revisit when.** Claude Desktop and/or Antigravity are checked the same way, or
Cursor ships resource support and this becomes moot.

## 2026-09-12 — An unrecognized language now returns `UNSUPPORTED_LANGUAGE`

**Context.** Running `02-surface.md` in Antigravity, E7 (`create_sandbox(language=
"klingon")`) expected `UNSUPPORTED_LANGUAGE` and got the generic `INVALID_INPUT`.
Pre-existing — I never touched this code path in v0.4.0's own work.

**Investigated.** `errors.UnsupportedLanguageError` was real and already used —
`native_runtime.py`'s `_spec()` raises it, and `server.py`'s deeper except block
already caught it — but `validate.language()`, the boundary check that actually
fires first on every real call, raised the generic `InvalidInput` instead.
Defense-in-depth for a divergence that never happens today, sitting behind a
front door that never opened it.

**Decided.** Fix `validate.language()` to raise `UnsupportedLanguageError`,
matching the message/fix shape `native_runtime._spec()` already uses for the
same condition, and widen `server.py`'s boundary `except` to catch it (it would
otherwise fall through to the generic exception handler further down — correct
behaviorally, since a tool call still can't crash, but with the wrong code
reaching the caller from the wrong except block).

**Because.** The whole design principle of structured errors is that an agent
branches on `error.code`, not on parsing English (`errors.py`'s own docstring).
`UNSUPPORTED_LANGUAGE` exists specifically so an agent can distinguish "you
named something that doesn't exist — pick from this list" from "you passed a
malformed argument." Collapsing that distinction at the one place it actually
mattered defeated the reason the class exists.

**Costs.** `validate.py`'s module docstring claimed "each raises `InvalidInput`"
— now false for one function, and the docstring says so explicitly rather than
silently going stale a second time.

**Caught by:** a real client (Antigravity) running the acceptance prompt, not
by any of the seven automated suites — `tests/verify.py` tested the runtime-level
raise directly (`rt.create(language="cobol", ...)`, bypassing the MCP boundary
entirely) and never checked which code came back from an actual tool call.
Added that check now.

**Revisit when.** Not expected.

## 2026-09-12 — FastMCP's update check phoned home on every server start

**Context.** Investigating a client that spawned servers in a retry loop, found
`fastmcp.settings.Settings` defaults: `check_for_updates="stable"` and
`show_server_banner=True`. The update check is an outbound HTTPS call to PyPI,
made once per server start. This machine's log has **835 server starts** in it.

**Why this matters beyond latency.** The no-egress rule is not a performance
preference, it is the product claim — nothing leaves the machine. A dependency
was quietly making a network call from the server process on every launch, and
nothing in seven acceptance suites looked for it. It is the same failure shape
as the doctor bug from earlier today: a real behaviour nobody had reason to
look at, because nothing asserts the absence of something.

**Measured, not assumed.** Installed binary, `initialize` round trip, 3 runs
each:

- default: 1.06s / 0.73s / 0.74s, mean **0.84s**
- `FASTMCP_CHECK_FOR_UPDATES=off`: 0.19s / 0.19s / 0.19s, mean **0.19s**

The spread in the default case is the network; the disabled case is flat. On a
machine with slow DNS this is startup time a client can time out waiting for.

**Decided.** `os.environ.setdefault` both to off in `server.py`, before the
`fastmcp` import — its `Settings` read the environment once, at import, so
anywhere later is too late.

`setdefault` rather than assignment: someone who genuinely wants either can set
it in their client config, and the override was verified to still win.

**Costs.** Two `os.environ` writes above an import, which needs a `noqa: E402`
and an explanation of why the ordering is load-bearing. Cheap. The banner also
goes away, which is pure gain on stdio — it rendered an ANSI box on stderr that
nobody reads.

**Not claimed.** This is *not* confirmed as the cause of the Antigravity
connection failure. It is a plausible contributor and a real defect in its own
right; the connection issue remains undiagnosed on the client side.

**Revisit when.** Not expected.

## 2026-09-12 — A running exec must never yield an exit code

**Context.** Running a real FastAPI backend through HyperBox surfaced that heavy
dependency installs were "impractical". Tracing why found something worse than
slowness: `api.exec_exit_code` read `.get("ExitCode") or 0`, and a running exec
has no exit code. An install the reader had given up on came back as a clean
success, `_provision`'s `if code != 0` passed, and `create()` sealed the sandbox
around a half-installed dependency set with no network left to repair it.

**Measured on both engines**, a `sleep 5` queried one second in:

```
docker  while running -> Running=True  ExitCode=None
podman  while running -> Running=True  ExitCode=0
```

**This is the part worth remembering.** The obvious fix — refuse when `ExitCode`
is `None` — is correct on Docker and useless on Podman, where a running exec is
indistinguishable from a successful one by exit code alone. It passed the Docker
gate and failed the Podman one. The check keys on `Running`.

That is failure pattern #2 from the blueprint, verbatim: *a property that holds
on one engine and not the other is invisible until it isn't*. It would have
shipped if the gate were one engine.

**Also fixed, same chain.** `_read_until_done` returned a partial result when it
gave up, indistinguishable from a clean finish — now raises, matching what a
socket does by itself on the other transport. And its budget was cumulative
(`started` set once) while the socket's is idle-based, so a twenty-minute
install progressing fine would be cut off on Windows and complete on Linux; it
now resets on every chunk, so both mean "no output for N seconds".

**Provisioning got its own budget.** `ENGINE_SOCKET_TIMEOUT` is derived from
`MAX_TIMEOUT_SECONDS`, which bounds AGENT CODE at 60s; nothing ever sized it for
an install. `policy.PROVISION_TIMEOUT_SECONDS = 900` on its own client, with no
`+ margin` — that margin exists to stop run()'s host-side deadline racing the
socket, and provisioning has no second clock to race.

**What the test had to reproduce.** Not a slow install — a SILENT one. Both read
paths give up on an idle stream, so a pip install streaming progress never trips
the budget however long it runs, which is correct. What tripped it in the field
was a native wheel compiling: minutes of real work with nothing on stdout. The
first version of the test used a real `pip install` and passed against the
unfixed code.

**Costs.** `exec_exit_code` now raises where callers previously got an int. Every
caller is in `native_runtime.py` and was audited; the run() path surfaces it as a
structured error instead of a false zero, which is the point.

**Revisit when.** Not expected.

## 2026-09-12 — A non-root image gets /sandbox, and keeps its non-root user

**Context.** Reported from a real run: a project Dockerfile ending `USER votify`
produced `PROVISION_FAILED: Could not find the file /sandbox in container`.

**Root cause.** `api.exec_create` never set `User`, so every exec inherited the
image's own. `mkdir -p /sandbox` therefore ran as that user, failed at the
filesystem root, and **its exit code was discarded** — as were the language
`setup` steps immediately after.

**Both engines were wrong, in different directions.** Measured:

```
docker: mkdir exit=1 Permission denied -> archive upload 404s -> create fails,
        blaming a missing file rather than the permission behind it
podman: mkdir exit=1 Permission denied -> archive API creates /sandbox ITSELF
        as root:root 0755 -> create SUCCEEDS, and the image's own user cannot
        write to it, so every run() fails later on a sandbox reported ready
```

Podman's is the worse one and would have been missed entirely by a Docker-only
gate — the second time in one day that the two engines disagreed about a failure
(see the `ExitCode` entry above).

**Decided.** Create `CODE_DIR` as root via an explicit `User` override, then
`chown` it to the image's configured user (`Config.User` off `inspect_container`).
Check that exit code and the setup steps'.

**Because.** The alternative — run everything as root — would make the symptom
disappear while silently undoing the hardening the Dockerfile asked for. Agent
code must still run as the image's user; it just needs a directory it can write
to. The acceptance test asserts BOTH halves, so a future "fix" that escalates to
root fails it.

**Costs.** `exec_create` grows a `user` parameter. It is passed only for
server-authored setup argv, never for anything a caller supplied, and the
docstring says so.

**Verified.** Built a real `USER appuser` image through HyperBox's own REST build
path and ran it end to end on both engines: create + sync + run, code executing
as `appuser`, `/sandbox` writable. Negative control reverted the fix and failed
on both — Docker at create, Podman at first use.

**Revisit when.** Not expected.

## 2026-09-12 — `.env.hyperbox` is the only dotenv that crosses, and it arrives as `.env`

**Context.** The field report complained that `.env` was blocked. Tracing the
filter found the opposite and worse defect: `buildcontext.sync_tar` matched
`path.name in deny_files` — exact strings — so `.env` was refused while
`.env.production`, `.env.local` and `.env.dev` synced straight into a sandbox
running generated code.

**Decided (owner's call).** Refuse every `.env*`, with exactly one exception:
`policy.SYNC_ENV_OPT_IN = ".env.hyperbox"`. It arrives renamed to `.env`, at the
same depth, so an app reading its normal config path needs no change. Both the
refusals and the rename are in the sync manifest.

**Because.** The name states the intent, so there is no heuristic about which
variants are "safe" and no new config format to learn — the person who writes
the file decides what is in it. The owner ruled out a broader `.hyperbox`
project config as overengineering, and it already has a designed home in 0.5's
`hyperbox init` preflight.

**Costs.** `.env.example` is now refused too, which is harmless (it holds no
secrets by convention) but might surprise. It is reported, not silent.

**A mistake worth recording.** The first patch replaced
`archive.add(path, arcname=relative)` with a `count=1` string replace — and hit
`build_tar`'s copy rather than `sync_tar`'s. Both functions had the identical
line. `build_tar` then referenced an `arcname` that does not exist in its scope:
a NameError that would have broken every `hyperbox build`, and it compiled
cleanly because Python resolves names at call time. Caught by running the
behaviour, not by the compiler. Patch by line number or unique context when two
functions share a line.

**Revisit when.** Not expected.

## 2026-09-12 — Say that a sandbox port is unreachable from the host

**Context.** An agent started a FastAPI app with `run(background=True)` and told
the user it was at `http://localhost:8000`. It was not: no port is published, so
a sandbox's `127.0.0.1` is a different loopback from the host's.

**The routing was one-directional.** Every surface said the sandbox cannot reach
OUT — `run`: "NO network access"; capabilities: `while_your_code_runs:
"disabled"`, "verified unreachable". Nothing anywhere said a listening port is
invisible to the host, while `127.0.0.1` appeared three times purely as an
affordance that works. An agent reading that reasonably concluded a URL was a
real thing to hand back.

**Decided.** State it where the agent reads: in `run`'s `background=True`
paragraph, and as `network.inbound_from_the_host` in capabilities. Plus a
troubleshooting entry and a paragraph in `agent-teams.md`. Asserted in
`verify.py` on the whitespace-normalised description, so it cannot drift out.

**Also.** `create_sandbox` deferred the `hyperbox build` command to
`hyperbox://capabilities` — a resource Cursor cannot read. So in the client where
the heavy-dependency wall was hit, the documented way around it was invisible.
The command is now in the tool description itself, alongside a note that a long
install can outlive the client's own tool-call timeout.

**Costs.** Tool descriptions get longer, and description length is real cost —
every token is read on every call. Judged worth it: this one caused an observed
false claim to a user.

**Revisit when.** Not expected.

## 2026-09-13 — Real-workload verification of Phases A–D: complete

**Context.** `fastapi-votify` re-run independently by the user in both Cursor
and Antigravity, against the actual fixed code (commit `46b3c8a` and after).

**Phase B — confirmed, strongly.** `Dockerfile` genuinely ends `USER votify`.
Both clients built the environment, synced the real project, and got FastAPI
fully running — health checks, Swagger, a real Spotify OAuth redirect. Zero
`PROVISION_FAILED`, zero permission errors, on exactly the class of image
Phase B exists for. `hyperbox ps` and both engines showed zero containers
left behind after each run.

**Phase C — confirmed.** A follow-up run with `.env.hyperbox` present and
`.env`/`.env.prod` excluded showed the manifest naming both exclusions
(`reason: "secret"`) and `/sandbox/.env` holding exactly `.env.hyperbox`'s
content, readable by `python-dotenv` inside the sandbox as intended.

**Phase A — indirectly exercised**, not stress-tested: no install in this
workload ran long enough to hit `PROVISION_TIMEOUT_SECONDS`. Covered instead
by the automated suite's silent-install case (`tests/verify.py`, watched
failing on both engines before the fix).

All four phases now have real-world evidence, not just synthetic tests.

## 2026-09-13 — `sync_from` walks the whole tree before filtering it

**Context.** The same run measured `create_sandbox` taking ~50s on a project
with a normal `.git` history and a `.venv` — no `.hyperboxignore` was present.
The manifest correctly listed every `.git/objects/...` path as skipped
(695KB of JSON), and 48 of the 50 seconds were the scan, not the container.

**Root cause, precisely.** `buildcontext.excluded()` already filters `.git`,
`__pycache__`, `.venv`, `venv`, `node_modules` unconditionally
(`ALWAYS_EXCLUDE`, buildcontext.py:21) — the exclusion logic is correct, and
`sync_tar` does call it. The cost is upstream of that: `sync_tar`'s walk is
`sorted(source.rglob("*"))`, which enumerates **every file on disk, including
every loose object in `.git`,** before any filter runs. `rglob` has no way to
prune a directory without first walking into it — unlike `os.walk`, which
lets a caller delete entries from `dirnames` in place and skip descending
entirely. So a correctly-excluded `.git` still costs a full stat() of every
object in it.

**Why this is not fixed now.** Explicit call: real, useful to fix, not
blocking a release whose whole purpose is closing correctness gaps found by
real use — this is a performance gap, discovered the same way, and deserves
the same rigor rather than a rushed same-day patch. Recorded precisely so a
future fix doesn't have to re-diagnose it: switch the walk to `os.walk` (or
an equivalent that supports directory pruning) and prune any `ALWAYS_EXCLUDE`
or `.hyperboxignore`-matched directory before descending into it, rather than
filtering its contents after enumerating them.

**Risk if unaddressed.** The user's run took 50s against a 60s-typical client
timeout — close enough that a slightly larger repo, or `node_modules`
present, would time out `create_sandbox` outright, and the failure would look
like a hang rather than name its cause.

**Decided.** Ship 0.4.0 as-is; land the fix early in 0.5 given the git-sync
work already there depends on exactly this walk. Recorded in the changelog's
0.4.1/0.5-planned notes so it isn't lost.

**Revisit when.** 0.5 Slice A (git sync in) begins — it already needs its own
walk for `git archive`, and this is the natural place to fix both at once.

## 2026-09-13 — The GC lag the user measured is the documented interval, not a bug

**Context.** With `HYPERBOX_TTL_SECONDS` set to 5 minutes for testing, a
sandbox showing `expired` in `hyperbox ps` took roughly 12 minutes total idle
time before its container actually disappeared.

**Verified, not assumed.** `policy.GC_INTERVAL_SECONDS = 300.0` — a fixed
5-minute sweep interval, independent of whatever TTL is configured. An
already-expired row is only reclaimed on the *next* sweep tick, so worst case
is TTL plus nearly one full interval before cleanup — with a 5-minute TTL,
up to ~10 minutes, which is what was observed (plus scheduling slack). This
is exactly the behaviour `06-garbage-collection.md` documents ("worst case is
TTL plus a few minutes, not TTL exactly") and not a caching artifact of
`hyperbox ps`, which reads the registry live on every call.

**Decided.** Not a bug; not fixed now. The interval is a fixed constant
specifically so GC has a bounded, predictable cost regardless of how many
sandboxes exist or what TTL each was given — making it track a short TTL
tightly would mean sweeping far more often for every user, most of whom use
the 30-minute default. Noted per the user's own call: worth revisiting if a
future release wants the sweep interval to scale with the shortest live TTL,
but that is a real design tradeoff (sweep cost vs. reclaim latency), not an
obvious win.

**Revisit when.** Someone reports the default (30 min TTL, 5 min sweep) lag
as a real problem, not just the deliberately-shortened test case here.

## 2026-09-13 — Product framing pivots from containment to attestation; `run_safely` gains `sync_from`

**Context.** Market/user research (competitive teardown of CodeRabbit, Qodo,
Greptile TREX, TestSprite, and a 2026 developer-trust survey) validated a
different pain point than the one HyperBox was built and marketed around.
96% of developers don't fully trust AI-generated code; only 48% actually
review it; 61% say it "often looks correct, but isn't." The root cause is
structural: the same model that writes a change is the only one that reports
whether it worked, with no independent check. HyperBox already returns
`{stdout, stderr, exit_code, success}` from a real container process rather
than from the agent's self-report — that property already existed, but the
README, PYPI.md and pyproject description all led with containment
("runs LLM-generated code... before it touches your project" / "doesn't
wreck your machine"), not with the trust property. Security containment is
real and load-bearing (see Threat model in CLAUDE.md) but is not why a
developer would install this.

**Options.** (1) Leave positioning as-is, treat this as a marketing-only
question to revisit later. (2) Reposition messaging only. (3) Reposition
messaging AND sharpen the one mechanism that already targets the new
framing — the `run_safely` prompt, which existed but was itself framed
around "unreviewed code" (security) rather than "don't claim success you
haven't seen" (attestation), and only worked against a retyped snippet, not
a real project.

**Decided.** (3). Reworded `run_safely`'s description and body around
attestation ("call this before telling the user a change works" /
"the model that wrote this code is not a credible witness to whether it
works"), and added an optional `sync_from` parameter so the same prompt can
verify a real edit against the real project (wired straight through to
`create_sandbox(sync_from=...)`, no new gate — reuses the existing
human-opt-in root list). Reworded the top of README.md, PYPI.md and
pyproject.toml's `description` to lead with the same framing, with
containment kept as a named side effect rather than deleted.

**Because.** The competitive teardown found "ground-truth execution as a
concept" already contested (Greptile TREX, TestSprite both execute for
real), but found nothing doing it locally, MCP-native, and mid-task —
called by the agent itself before it asserts anything to the developer,
rather than at PR/CI time after a diff already exists. That gap is real but
not durable, and `run_safely` was the one piece of surface already aimed in
roughly the right direction — sharpening it cost a doc/prompt change, not
new engine work, so there was no reason to wait on the messaging-only
option.

**Costs.** The project's headline test suite (`verify_containment.py`) and
its "It contains hostile code" section now sit slightly out of step with the
lead pitch — still true and still worth having, but a reader hitting that
section right after the new opening may read it as a return to the old
framing. Not fixed in this pass; the section itself is accurate and didn't
need touching, only its prominence might, later. `run_safely`'s `sync_from`
plumbing has not yet been exercised end-to-end against a real container with
a real test suite (`pytest`/`npm test`/etc.) — verified only that the prompt
renders correctly via `Client(server.mcp)`, not that the resulting agent
workflow actually surfaces a trustworthy pass/fail from a real project.

**Revisit when.** After the section reordering is tested with real users
(the planned Show HN / subreddit posts) — if "It contains hostile code"
reads as a non-sequitur to a fresh reader, move or trim it. Also revisit if
a user tries `run_safely(sync_from=...)` against a real test suite and hits
friction — that's the first real signal on whether the prompt-level nudge is
enough or whether this needs to become a first-class tool argument instead.
