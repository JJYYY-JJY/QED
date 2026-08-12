# QED v2 stable candidate scorecard

> Generated from `v2-stable-evidence.json`; scores are not hand-entered.
> Any required non-passed gate keeps that dimension below 10/10.

| Dimension | Eligible for 10/10 | Required gate summary |
| --- | --- | --- |
| 软件架构 (`architecture`) | NO | architecture.roadmap=passed, architecture.state-policy-tests=passed, architecture.dependency-boundaries=passed, architecture.api-sse-contract=passed, architecture.offline-bundle-contract=passed, architecture.coverage=failed, architecture.mutation=failed |
| 安全与审计 (`security`) | NO | security.path-network-regressions=passed, security.protocol-limits=passed, security.offline-bundle=passed, security.baseline-scan-closeout=blocked, security.secret-export-scan=passed |
| 数学正确性保障 (`mathematics`) | NO | mathematics.policy-n-of-n=passed, mathematics.claim-graph=passed, mathematics.fresh-thread-lineage=passed, mathematics.benchmark-lock=passed, mathematics.statistics-tests=passed, mathematics.real-reliability-window=blocked, mathematics.sealed-holdout=blocked, mathematics.mutation-and-citation-metrics=unrun |
| 稳定版成熟度 (`maturity`) | NO | maturity.alpha-tool-removal=passed, maturity.migration-recovery=passed, maturity.fault-injection-concurrency=unrun, maturity.golden-run=passed, maturity.doctor=failed, maturity.frontend-and-e2e=passed, maturity.ci-platform-and-stability=unrun, maturity.real-codex-canary=blocked, maturity.real-codex-conformance=blocked, maturity.release-documentation=passed |

Current result: no dimension is eligible for 10/10. The real Codex reliability window, sealed holdout, final security closeout, coverage/mutation gates, platform matrix, and stability run remain blockers where marked.

QED policy PASS is a code-computed policy decision. It is not formal verification, a mathematical truth claim, a signature, or a trusted timestamp.
