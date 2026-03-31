# Calculator — Anvil Example

Try the full anvil workflow on this tiny project.

## Setup

```bash
cd examples/calculator
git init && git add . && git commit -m "init"
```

## Run

```bash
# Preview the plan
anvil-build --dry-run

# Build it — your LLM codes, Claude reviews
anvil-build
```

The plan has 2 tasks. Your LLM writes `calculator.py` and `test_calculator.py`. Claude reviews each one.
