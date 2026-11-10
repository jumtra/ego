# Fully offline integration example

This example does not call any AI provider. It demonstrates initialization, checkpointed DAG work, verification, evaluation, memory and local code search using the deterministic `mock` driver. Copy it into a scratch folder before running `--apply`.

```bash
python3 -m ego init --root . --apply
python3 -m ego work run --root . --task work-task.json --host mock --apply --approve
python3 -m ego eval run --root . --suite eval-suite.json --host mock
python3 -m ego doctor --root .
```

The generated `result.txt` should contain `OK`. A mocked eval is a software integration test, **not** evidence of an actual model improving its prompting behavior.
