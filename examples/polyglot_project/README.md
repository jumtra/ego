# Polyglot demo (read-only discovery)

Run `seh project detect --root examples/polyglot_project` and `seh project checks --root examples/polyglot_project`.

This fixture intentionally requires no installed Node/Go/Zig. To execute checks, install the real toolchain,
review repository scripts and explicitly use `seh project check ... --execute --approve`.
The custom Zig entry demonstrates a language extension without editing the harness's Python code.
