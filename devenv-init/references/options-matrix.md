# Options Matrix

Per-template default values for `features.*.enable`. Override with `--with-*` / `--no-*` flags.

| Toggle | `python` | `python+django` | `python+fastapi` | `python+flask` |
|---|---|---|---|---|
| `direnv` | true | true | true | true |
| `delta` | true | true | true | true |
| `treefmt` | true | true | true | true |
| `git-hooks` | true | true | true | true |
| `lsp` | false | false | false | false |
| `strict-types` | false | false | false | false |
| `services.postgres` | false | **true** | false | false |
| `services.mysql` | false | false | false | false |
| `services.redis` | false | false | false | false |
| `services.sqlite` | true | false | true | true |

## Flag → file mapping

| Flag | Patches |
|---|---|
| `--with-*` / `--no-*` | `devenv.nix` `features.<key>.enable` |
| `--python <ver>` | `devenv.nix` `languages.python.version`; `pyproject.toml` `requires-python` + `target-version`; `pyrightconfig.json` `pythonVersion` |
| `--name <name>` | `pyproject.toml` `name` + `packages` |
| `--strict-types` | additionally flips `pyrightconfig.json` `typeCheckingMode` to `strict` |
