# Contributing

Read [AGENTS.md](AGENTS.md) before changing QED. It defines the runtime,
persistence, verifier, and safety invariants that each change must preserve.

## Set up

QED supports Python 3.13 and 3.14. uv manages the interpreter, environment,
lockfile, dependencies, and commands:

```bash
uv sync --all-groups --frozen
npm ci
```

Do not create a Conda environment or install project packages with ad hoc `pip`
commands. Change `pyproject.toml`, refresh `uv.lock` with uv, and include both
files when dependency metadata changes. Change `package.json`, refresh
`package-lock.json` with npm, and include both files for client dependencies.

The removed alpha-only visual tooling is not a current development or release
dependency. QED's service and console runtime use the repository's ordinary
typed tests and accessibility checks.

## Change process

1. State the behavior and a check that can prove it.
2. Add or update a test that fails for the missing behavior.
3. Make the smallest code change that passes the test.
4. Run the narrow test, then the full checks that cover the changed boundary.
5. Update architecture, configuration, migration, threat-model, and research
   notes when their contracts change.

Keep unrelated formatting, refactors, and dead-code cleanup out of the diff.
Retain the MIT license, upstream attribution, prompt archive, proved statements,
expert commentary, and historical artifacts.

## Architecture rules

- Route all Codex work through `qed.runtime.CodexRuntime`.
- Derive model control output from strict Pydantic models and JSON Schema.
- Let `qed.store` own transactions, transitions, event sequence numbers,
  immutable records, and execution fencing.
- Keep sealed candidates and verifier reports unchanged across resume.
- Start every required verifier role on a fresh read-only thread.
- Keep structural and detailed verification offline. Restrict search and network
  to literature and citation work.
- Compute PASS in application code from stored reports.
- Call the application service from both HTTP and CLI surfaces.
- Keep secrets outside run models, browser responses, logs, and exports.

Do not add provider dispatch, Claude, Gemini, full-access sandboxes, approval
bypasses, raw Codex overrides, browser credentials, or Markdown verdict parsing.

## Checks

Run the backend suite before opening a pull request:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
uv build
```

The checked-in pytest configuration excludes `real_codex` tests. Run a real
smoke test only when the change needs it, the test has the marker, and you have
approved the credential, network, quota, and cost impact:

```bash
uv run pytest -m real_codex
```

Never put a live smoke test in the default suite or CI.

Frontend changes also run:

```bash
npm run lint
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

The browser console must not accept, store, or send the QED API bearer token.
Use a tokenless loopback API during local work. A remote browser deployment
needs an external HttpOnly session layer that keeps the bearer token
server-side.

## Runtime research

Codex models, effort values, SDK surfaces, App Server methods, hooks, skills,
and sandbox behavior can change. Use official OpenAI sources for those facts.
Record the source URL, access date, inspected version, decision, and bounded
uncertainty under `docs/research/`.

Pin Python dependencies in `uv.lock`. Pin GitHub Actions to commit SHA values
and keep the release tag in a comment. Review generated lockfile and action
changes before merge.

## Pull-request checklist

- [ ] Each changed line supports the stated behavior.
- [ ] New behavior has a failing-then-passing test.
- [ ] Sealed records, fresh verifier identity, and code PASS remain intact.
- [ ] No credential, unsafe flag, raw override, or provider dispatch entered
      the diff.
- [ ] Backend lint, typecheck, tests, and package build pass.
- [ ] Frontend lint, typecheck, tests, build, accessibility, and Playwright pass
      when client code changed.
- [ ] Contract changes include matching docs and threat-model updates.
- [ ] Historical assets and upstream attribution remain present.
- [ ] The branch contains logical commits and no generated runtime state.

Do not push, publish, or open an external pull request unless the repository
owner asks for that action.
