# Role-specific model design for a post-alpha schema

QED v2 alpha uses `QEDConfig.model` for literature, planning, proof,
verification, and adjudication. This keeps one tested capability resolution and
one migration contract. The alpha does not include role overrides.

## Proposed configuration

`QEDConfig` schema v2 would replace the single required model string with a
default and optional overrides:

```json
{
  "schema_version": 2,
  "models": {
    "default": "gpt-5.6-sol",
    "literature": null,
    "planner": null,
    "prover": null,
    "structural_verifier": null,
    "detailed_verifier": null,
    "citation_verifier": null,
    "adjudicator": null
  }
}
```

A null override inherits `default`. The API and CLI must reject unknown role
keys and blank model names. Effort remains global until QED has evidence that
role-specific effort produces a useful and supportable contract.

## Persistence and migration

Stored schema-v1 configuration JSON and its hash stay unchanged. Readers parse
v1 and v2 as a versioned union. They resolve every v1 role to the existing
`model` field. A database migration must not rewrite historical configuration,
turn input, provenance, or manifest bytes.

Before the first model turn, the runtime probes each distinct resolved model and
persists a content-addressed capability map in the execution resolution. A
missing model or incompatible control stops the run before research work.
Resume re-probes the same frozen model names and fails closed on capability
drift.

Each `runtime.turn_started` event must record the resolved role and actual model.
Thread records already retain a model; manifest turns need an explicit model
field so readers do not infer it from a global default. Export must compare the
event, thread, provenance, and capability-map values.

## Acceptance tests

A later implementation needs these tests before merge:

- schema v1 produces the same config hash and resolves every role to its frozen
  global model;
- schema v2 rejects unknown roles and records the default or exact override for
  all seven roles;
- capability probing deduplicates identical model names and stops before the
  first turn when any selected model is unavailable;
- every thread, turn event, snapshot, and manifest records the actual model;
- resume rejects model or capability drift without rewriting prior turns;
- prover and verifier external-thread isolation still applies when their model
  names differ or match;
- SDK, App Server, and explicitly selected exec routes receive the resolved
  model for each role;
- exports from schema-v1 runs remain byte-for-byte reproducible.

Model diversity can reduce some correlated failures, but a different model name
does not establish independent training data, peer review, or mathematical
truth.
