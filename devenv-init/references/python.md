# Python templates

Four templates: `python`, `python+django`, `python+fastapi`, `python+flask`.

## Tools bundled

| Tool | Source | Purpose |
|---|---|---|
| Python 3.12 | `languages.python.uv` | runtime |
| `uv` | devenv | package manager (replaces pip/poetry) |
| `ruff` | `pkgs.ruff` | linter + formatter |
| `basedpyright` | `pkgs.basedpyright` | type checker / LSP |
| `treefmt` + `nixfmt-rfc-style` | `features.treefmt` | multi-language formatter |
| git-hooks (pre-commit) | `features.git-hooks` | ruff + ruff-format + nixfmt |

## After scaffold

```bash
mkdir -p src/$(grep -oE 'src/[a-z_]+' pyproject.toml | head -1 | sed 's|src/||') && touch src/$(grep -oE 'src/[a-z_]+' pyproject.toml | head -1 | sed 's|src/||')/__init__.py

direnv allow                # or: devenv shell
uv sync                     # install deps from pyproject.toml
```

Verify:

```bash
python --version
ruff --version
basedpyright --version
```

## Database details

These are wired by the corresponding `features.services.<db>` modules in `devenv-templates`.

### Postgres (`--with-postgres`)

- Adds `pkgs.postgresql` and `pkgs.libpq` to `packages`.
- Sets `LD_LIBRARY_PATH` so `psycopg[binary]` / `psycopg2` find libpq at runtime.
- Starts a local postgres process via `services.postgres` (toggle `runService = false` in devenv.nix to skip).
- For `python+django`: `psycopg[binary]>=3.1` is in `pyproject.toml` deps.

### MySQL (`--with-mysql`)

- Adds `libmysqlclient`, `libmysqlclient.dev`, `pkg-config`, `openssl`.
- Sets `MYSQLCLIENT_CFLAGS` and `MYSQLCLIENT_LDFLAGS` for compiling `mysqlclient` Python package.
- Starts a local mysql process via `services.mysql`.

### Redis (`--with-redis`)

- Starts redis via `services.redis`. No build env needed for Python clients.

### SQLite (`--with-sqlite`)

- Adds `pkgs.sqlite` for the CLI. Python's stdlib `sqlite3` works without extra setup.

## Strict types (`--strict-types`)

Patches `pyrightconfig.json`:

```json
"typeCheckingMode": "strict"
```

Enable when you want maximum static safety. Note that with `basedpyright` even `standard` mode is stricter than upstream pyright's `standard`.

## Framework-specific notes

### Django (`python+django`)

- `django>=5.0`, `djangorestframework`, `django-filter`, `psycopg[binary]`
- `django-stubs` in dev deps
- `tool.ruff.lint.isort.sections.django` configured for proper import ordering
- `tool.django-stubs.django_settings_module = "config.settings"` — adjust if your settings live elsewhere
- Postgres ON by default

### FastAPI (`python+fastapi`)

- `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`
- `httpx` and `pytest-asyncio` in dev
- `asyncio_mode = "auto"` in pytest config
- SQLite ON by default (swap with postgres if you need it)

### Flask (`python+flask`)

- `flask>=3.0`, `flask-sqlalchemy`, `flask-migrate`
- `pytest-flask` in dev
- SQLite ON by default

## Common workflow rules

Per `~/.claude/rules/python.md`: **never edit `pyproject.toml` `dependencies` directly**. Use:

```bash
uv add <package>            # add a runtime dep
uv add --dev <package>      # add a dev dep
uv remove <package>
```
