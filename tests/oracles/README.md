# Oracle runners

Adapters for third-party tools that cannot be driven from Python.

`eslint-plugin-sonarjs` is **LGPL-3.0**. It is invoked here as a subprocess in CI only, never
linked into or distributed with OXN, which keeps it licence-clean under
[ADR-0001](../../docs/adr/0001-dependency-policy.md).

To run the JavaScript oracle lane locally:

```sh
cd tests/oracles
npm install --no-save eslint@9 eslint-plugin-sonarjs@4
export OXN_SONARJS_DIR="$PWD"
pytest -m oracle
```

Without `OXN_SONARJS_DIR`, those tests skip.
