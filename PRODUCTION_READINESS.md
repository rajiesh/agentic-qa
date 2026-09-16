# Production Readiness Checklist

Tracked findings from a three-track audit (codebase security/architecture, packaging/CI/docs,
and external best-practice research) on what agentic-qa needs before release to QA engineers
and developers. Full narrative version with sources: https://claude.ai/artifact/27gmjJRtFFxb3vcJ22P1ZP

Check items off as they're fixed. Each has a `Source:` tag noting which track surfaced it.

## Suggested sequence

1. **This week, in parallel:** the 3 security fixes and the 4 packaging basics below — none touch each other.
2. **Next:** decide the deployment story explicitly (personal CLI per engineer, vs. a shared service) — shapes the secrets/multi-user work.
3. **Then, the actual product change:** a human review/approval gate before generated tests are trusted, plus assertion-strength/flakiness checks in the validator.

---

## Critical — resolve before any release

- [ ] **File tools never check paths stay inside the repo.** `read_file`, `list_directory`,
  `search_code`, and every write path (`write_tools.py`, `OutputManager.write`) join a
  caller-given path onto `repo_root` without checking it stays there. Paths are LLM-chosen,
  and repo content can prompt-inject the model into steering them — a live arbitrary
  file read/write vector.
  Fix: one shared helper that resolves the joined path and rejects anything not
  `is_relative_to(repo_root.resolve())`, used at all four call sites.
  *Source: security audit*

- [ ] **Git credentials leak into logs and error output.** A token embedded in a clone URL
  gets logged verbatim, and a failed clone's `CalledProcessError` repr includes the full argv.
  Fix: strip userinfo from URLs before logging/raising; document a token-free auth path
  (SSH key, `GIT_ASKPASS`).
  *Source: security audit*

- [ ] **Doc-link fetcher has no SSRF protection.** `async_fetch_url` has no scheme/host
  allowlist and doesn't block private/link-local IPs (incl. cloud metadata endpoints).
  Fix: restrict to http(s); block private/reserved IP ranges before fetching.
  *Source: security audit*

- [ ] **No CI pipeline.** `.github/workflows/` holds only an auto-generated status bot —
  nothing runs tests/lint/type-checks on push or PR, no release automation.
  Fix: add a workflow running `pytest --cov`, `ruff check`, `mypy --strict` on every PR,
  plus a tag-triggered publish job.
  *Source: packaging audit*

- [ ] **No LICENSE file.** Legally ambiguous for anyone else to use/redistribute — blocks
  any public or commercial release outright.
  Fix: add MIT or Apache-2.0.
  *Source: packaging audit*

- [ ] **Not packaged for distribution.** Version pinned at `0.1.0`, deps unbounded (`>=`
  only, no lockfile), no CHANGELOG, no PyPI publish step, no Dockerfile — only install path
  is `git clone` + editable install.
  Fix: publish to PyPI, cap/lock dependency ranges, add a Dockerfile and CHANGELOG.
  *Source: packaging audit*

- [ ] **No `--version` flag.** Despite a full Typer/Rich CLI, no way to check the installed
  version — basic hygiene for bug reports.
  Fix: add `agentic-qa --version`, reading from package metadata.
  *Source: packaging audit*

- [ ] **One shared API key, no per-user model.** A single `ANTHROPIC_API_KEY` feeds one
  `AsyncAnthropic` client process-wide — no per-user credential scoping or session isolation.
  Fine for a personal CLI; not a shared-service model as-is.
  Fix: decide the target explicitly — "one install per engineer" documented, or per-user
  credential scoping / a proxy backend for team deployment.
  *Source: packaging audit*

- [ ] **Generated-test quality checking stops at lint.** `PostGenerationValidator` runs
  ruff/eslint plus a file-existence check — nothing checks assertion strength or flakiness.
  A 204,673-file study found agent-generated tests carry ~8× the weak/unrecognized-assertion
  rate (11.6% vs 1.5%) and materially higher non-determinism than human-written tests.
  Fix: add assertion-strength and flakiness detection (repeat-run the generated suite N
  times) to the validator, not just static lint.
  *Source: external research*

- [ ] **Nothing gates generated tests before they're trusted.** Today's framing is
  implicitly "generate and done." Research consensus: this class of tool is a high-volume
  draft generator needing human review/approval before its output becomes a CI quality
  gate — not an autonomous replacement for QA judgment.
  Fix: add a review/approve step between generation and a target repo's CI.
  *Source: external research*

## Important — plan for the next milestone

- [ ] **Git subprocess calls have no timeout.** `_git`/`_git_raw` can hang forever (auth
  prompt, slow host), unlike the ripgrep helper (15s timeout).
  Fix: add a configurable timeout; handle `TimeoutExpired` explicitly.
  *Source: security audit*

- [ ] **`max_repo_size_mb` is checked after the fact.** Size is measured only once a full
  `--depth 1` clone has completed — a huge blob is fully downloaded before the cap applies.
  Fix: use `git clone --filter=blob:limit=…` or probe size before full checkout.
  *Source: security audit*

- [ ] **A stale clone is silently reused.** If `git pull --ff-only` fails, the run logs a
  warning and analyzes the old checkout anyway.
  Fix: fail the run (or force a fresh clone) instead of proceeding on a stale checkout.
  *Source: security audit*

- [ ] **Checkpoints hold repo content in plaintext.** Scan results/architecture summaries
  can contain proprietary code snippets, endpoint/env-var names, unencrypted under
  `output_dir`.
  Fix: document the exposure explicitly, or encrypt checkpoint contents at rest.
  *Source: security audit*

- [ ] **README has no troubleshooting or contribution guide.** Install/quickstart/CLI
  reference are solid; nothing on rate limits, budget-exceeded behavior, network failures;
  no CONTRIBUTING.md.
  Fix: add both sections; link a CI badge once CI exists.
  *Source: packaging audit*

- [ ] **Error messages are inconsistent.** CLI maps config/budget errors to clear text, but
  `session.py`'s main loop prints raw exception text for anything else.
  Fix: map common failure classes (network, auth, malformed YAML) to friendly guidance in
  both entry points.
  *Source: packaging audit*

- [ ] **The riskiest code is the least tested.** No dedicated tests for `cli.py`,
  `orchestrator.py`, `platform_orchestrator.py`, or most agents (strategist, scanner,
  synthesizer, session agent, the seven specialists).
  Fix: start with `test_cli.py` (exit codes, `--help`, error paths) and mocked-client
  orchestrator integration tests.
  *Source: packaging audit*

- [ ] **No per-call tracing — cost tracking isn't observability.** `CostTracker` aggregates
  USD spend for a whole run; no per-call trace (model, prompt version, tools, tokens, cost
  per span), no trajectory viewer.
  Fix: emit a structured span per API call, keyed by agent/run.
  *Source: packaging audit, external research*

- [ ] **No ongoing evaluation of generated-test quality.** Industry survey: 89% of orgs
  running agents in prod have observability, only 52% have eval practices; 32% name quality
  as the top barrier. agentic-qa generates once and stops.
  Fix: add a recurring eval pass (or sampled human review) over generated tests.
  *Source: external research*

- [ ] **No quarantine for flaky generated tests.** Intermittent failures across repeat runs
  aren't flagged or isolated — they erode trust in the whole suite.
  Fix: track pass/fail history per generated test; auto-quarantine ones that flip.
  *Source: external research*

- [ ] **Test planning isn't explicitly risk-weighted.** The E2E entry is already gated on
  detecting a real frontend (good precedent), but functional/integration/contract scoping
  doesn't extend that "prioritize critical paths / high-risk integration points" logic.
  Fix: carry the E2E-gate's risk logic into how every test type is scoped.
  *Source: external research*

## Nice-to-have — worth doing, doesn't block a release

- [ ] **`write_tools.py` looks unused.** Specialists write through `OutputManager.write`
  instead, which has a different signature.
  Fix: remove it, or wire it in if it's meant to be used.
  *Source: security audit*

- [ ] **`--run-tests` is marked reserved.** `executor_tools.py`/`config.run_generated_tests`
  aren't confirmed wired end-to-end.
  Fix: finish the flag or remove it from the CLI.
  *Source: security audit*

- [ ] **No proactive rate limiting.** At `repo_concurrency_limit=5` ×
  `scanner_concurrency_limit=10`, nothing throttles ahead of Anthropic's rate tier — relies
  entirely on reactive 429 backoff.
  Fix: add a shared token-bucket ahead of `messages.create` at scale.
  *Source: security audit*

- [ ] **No Dockerfile or devcontainer.** Trying agentic-qa still means setting up a local
  Python 3.12 environment first.
  Fix: add a minimal Dockerfile for a zero-setup trial path.
  *Source: packaging audit*

- [ ] **Lint/type-checking configured but not enforced.** `ruff`/`mypy --strict` are set up
  in `pyproject.toml`, but with no CI and no pre-commit hook, nothing stops drift.
  Fix: add a pre-commit config once CI exists.
  *Source: packaging audit*

- [ ] **`platform.yaml` has no schema validation.** Plain dict `.get()`/`KeyError` access —
  a malformed file surfaces a raw traceback instead of a clean validation error.
  Fix: validate through a Pydantic model.
  *Source: packaging audit*

## Already validated — don't rebuild this

- **The scanner-then-synthesizer split holds up.** Reasoning over compact ~600-token
  service summaries instead of raw file content at platform scale is directly supported by
  long-context-degradation research — extend this pattern, don't replace it.
  *Source: external research*

- **Secondary agents already inherit the context-window fix.** `ServiceScannerAgent`,
  `PlatformSynthesizerAgent`, and `PlatformStrategistAgent` all extend `BaseAgent` and call
  the shared loop, so this session's earlier proactive-eviction / reactive shrink-then-error
  fix already protects them.
  *Source: security audit*
