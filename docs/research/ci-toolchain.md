# CI toolchain decision

Research date: 2026-07-16 (America/Los_Angeles)

## Decision

GitHub Actions runs the locked backend on Python 3.13 and 3.14. The default
pytest configuration excludes real Codex tests, so pull requests do not need a
credential and do not spend model quota.

The workflow grants `contents: read`, disables persisted checkout credentials,
sets a timeout, cancels superseded runs, and pins third-party actions to commit
SHA values. Release tags remain as comments for update review.

## Action evidence

The workflow pins:

| Action | Release | Commit |
| --- | --- | --- |
| `actions/checkout` | [`v7.0.0`](https://github.com/actions/checkout/releases/tag/v7.0.0) | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` |
| `astral-sh/setup-uv` | [`v8.3.2`](https://github.com/astral-sh/setup-uv/releases/tag/v8.3.2) | `11f9893b081a58869d3b5fccaea48c9e9e46f990` |
| `actions/setup-node` | [`v6.5.0`](https://github.com/actions/setup-node/releases/tag/v6.5.0) | `249970729cb0ef3589644e2896645e5dc5ba9c38` |

The official [setup-uv documentation](https://github.com/astral-sh/setup-uv)
supports a Python-version matrix and recommends a full action commit pin. QED
also pins the installed uv binary to `0.11.29`; dependency resolution then uses
the checked-in `uv.lock` with `--frozen`.

GitHub's [checkout documentation](https://github.com/actions/checkout) lists
`contents: read` as the recommended token permission. QED also sets
`persist-credentials: false` because CI performs no authenticated Git command
after checkout.

The official [Node.js 22.23.1 archive](https://nodejs.org/en/download/archive/v22.23.1)
records the exact client runtime selected for CI. The official
[`setup-node` v6.5.0 release](https://github.com/actions/setup-node/releases/tag/v6.5.0)
binds that runtime setup to the pinned action commit above.

## Required backend gates

Each supported Python version runs:

```text
uv sync --all-groups --frozen
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest
```

Python 3.14 also runs `uv build`. The matrix proves the package contract
`>=3.13,<3.15` rather than testing only the local `.python-version` pin.

## Required frontend gates

The client job uses Node 22.23.1, which satisfies the checked-in `>=22.12`
engine contract, and installs the root `package-lock.json` with `npm ci`. It
runs:

```text
npm run lint
npm run typecheck
npm test
npm run test:impeccable
npm run build
npm run impeccable
npx playwright install --with-deps chromium
npm run test:e2e
```

The Impeccable regression command exercises the checked-in local-tool security
boundary. The detector command runs without `npx` or network access. Playwright
installs Chromium because both configured projects use that browser engine at
desktop and narrow viewport sizes.

The job also rejects `Authorization`, `Bearer`, token-storage keys, and Vite
secret variables in production browser source. Regression tests may name those
strings to assert their absence from requests. The browser console runs without
a token on loopback or uses an external HttpOnly session layer for remote
deployment.

## Update policy

An action update requires an official release-page check, a new full commit
SHA, and workflow validation. A uv or Node update requires the matching locked
install and all language gates. Keep real-model smoke tests in an opt-in
workflow or an operator-run command with the `real_codex` marker.
