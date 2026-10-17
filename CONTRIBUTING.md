# Contributing

Thanks for looking. Ground rules:

1. **Honesty labels are sacred.** A connector is `LIVE` only if its adapter
   really performs the read/write (a local stand-in is fine). Anything else
   is `SCAFFOLD`, in code, docs, and UI.
2. **No secrets, ever.** Not in code, tests, fixtures, or examples. Secret
   material is always a reference (`env:`, `vault:`, `secret:`). Test
   fixtures use obviously fake values like `env:DEMO_REF`.
3. **No dataclasses for domain/store objects** — validated Pydantic v2
   models only. Objects that need a store handle take it via an explicit
   `__init__(_store=None, **data)` and assign after `super().__init__`.
4. **Audit everything you add.** New state-changing behavior needs an audit
   event (see AUDIT.md for the taxonomy) and a test.
5. **Fail closed.** Missing auth config, unknown agents, disallowed
   skills/connectors, and non-read-only data statements are errors, not
   warnings.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
uvicorn app.main:build_app --factory --port 8090
```

## Adding a connector

See CONNECTORS.md — YAML + adapter, contract tests parametrize over the
catalog automatically.
