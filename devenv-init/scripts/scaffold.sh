#!/usr/bin/env bash
set -euo pipefail

LANG_ARG=""
FRAMEWORK=""
PYTHON_VERSION="3.12"
PROJECT_NAME=""
WITH_DIRENV=true
WITH_DELTA=true
WITH_TREEFMT=true
WITH_GIT_HOOKS=true
WITH_LSP=false
STRICT_TYPES=false
WITH_POSTGRES=""
WITH_MYSQL=""
WITH_REDIS=""
WITH_SQLITE=""

usage() {
  cat <<'EOF'
Usage: scaffold.sh --lang <lang> [--framework <fw>] [options]

Required:
  --lang {python}                       Language (more added over time)

Common:
  --framework {django|fastapi|flask}    Framework variant (Python only for now)
  --python {3.10|3.11|3.12|3.13}        (default: 3.12)
  --name <name>                         Project name (default: dirname)

Toggles (omit = template default):
  --with-direnv     / --no-direnv         (default ON)
  --with-delta      / --no-delta          (default ON)
  --with-treefmt    / --no-treefmt        (default ON)
  --with-git-hooks  / --no-git-hooks      (default ON)
  --with-lsp        / --no-lsp            (default OFF)
  --strict-types    / --no-strict-types   (default OFF; flips pyrightconfig)
  --with-postgres   / --no-postgres
  --with-mysql      / --no-mysql
  --with-redis      / --no-redis
  --with-sqlite     / --no-sqlite

Examples:
  scaffold.sh --lang python --framework django --with-postgres
  scaffold.sh --lang python --python 3.13 --strict-types --name my-cli
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --lang)            LANG_ARG="$2"; shift 2 ;;
    --framework)       FRAMEWORK="$2"; shift 2 ;;
    --python)          PYTHON_VERSION="$2"; shift 2 ;;
    --name)            PROJECT_NAME="$2"; shift 2 ;;
    --with-direnv)     WITH_DIRENV=true; shift ;;
    --no-direnv)       WITH_DIRENV=false; shift ;;
    --with-delta)      WITH_DELTA=true; shift ;;
    --no-delta)        WITH_DELTA=false; shift ;;
    --with-treefmt)    WITH_TREEFMT=true; shift ;;
    --no-treefmt)      WITH_TREEFMT=false; shift ;;
    --with-git-hooks)  WITH_GIT_HOOKS=true; shift ;;
    --no-git-hooks)    WITH_GIT_HOOKS=false; shift ;;
    --with-lsp)        WITH_LSP=true; shift ;;
    --no-lsp)          WITH_LSP=false; shift ;;
    --strict-types)    STRICT_TYPES=true; shift ;;
    --no-strict-types) STRICT_TYPES=false; shift ;;
    --with-postgres)   WITH_POSTGRES=true; shift ;;
    --no-postgres)     WITH_POSTGRES=false; shift ;;
    --with-mysql)      WITH_MYSQL=true; shift ;;
    --no-mysql)        WITH_MYSQL=false; shift ;;
    --with-redis)      WITH_REDIS=true; shift ;;
    --no-redis)        WITH_REDIS=false; shift ;;
    --with-sqlite)     WITH_SQLITE=true; shift ;;
    --no-sqlite)       WITH_SQLITE=false; shift ;;
    -h|--help)         usage; exit 0 ;;
    *)                 echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [ -z "$LANG_ARG" ]; then
  echo "error: --lang is required" >&2
  usage
  exit 2
fi

TEMPLATE="$LANG_ARG"
if [ -n "$FRAMEWORK" ]; then
  TEMPLATE="$LANG_ARG+$FRAMEWORK"
fi

if [ -z "$PROJECT_NAME" ]; then
  PROJECT_NAME="$(basename "$PWD")"
fi

echo "[devenv-init] template: $TEMPLATE"
echo "[devenv-init] cwd:      $PWD"
nix flake init -t "github:Eotel/devenv-templates#$TEMPLATE"

patch_feature() {
  local key="$1" val="$2"
  if [ -z "$val" ]; then return 0; fi
  if [ ! -f devenv.nix ]; then return 0; fi
  sed -i.bak -E "s#(${key}\\.enable[[:space:]]*=[[:space:]]*)(true|false)(;)#\\1${val}\\3#" devenv.nix
}

patch_feature "direnv" "$WITH_DIRENV"
patch_feature "delta" "$WITH_DELTA"
patch_feature "treefmt" "$WITH_TREEFMT"
patch_feature "git-hooks" "$WITH_GIT_HOOKS"
patch_feature "lsp" "$WITH_LSP"
patch_feature "strict-types" "$STRICT_TYPES"
patch_feature "services\\.postgres" "$WITH_POSTGRES"
patch_feature "services\\.mysql" "$WITH_MYSQL"
patch_feature "services\\.redis" "$WITH_REDIS"
patch_feature "services\\.sqlite" "$WITH_SQLITE"

if [ "$LANG_ARG" = "python" ]; then
  PY_NUM="${PYTHON_VERSION//./}"
  if [ -f devenv.nix ]; then
    sed -i.bak -E "s#(languages\\.python\\.version[[:space:]]*=[[:space:]]*)\"[0-9.]+\"#\\1\"${PYTHON_VERSION}\"#" devenv.nix
  fi
  if [ -f pyproject.toml ]; then
    sed -i.bak -E "s#(requires-python[[:space:]]*=[[:space:]]*\")>=[0-9.]+(\")#\\1>=${PYTHON_VERSION}\\2#" pyproject.toml
    sed -i.bak -E "s#(target-version[[:space:]]*=[[:space:]]*\")py[0-9]+(\")#\\1py${PY_NUM}\\2#" pyproject.toml
  fi
  if [ -f pyrightconfig.json ]; then
    sed -i.bak -E "s#(\"pythonVersion\"[[:space:]]*:[[:space:]]*\")[0-9.]+(\")#\\1${PYTHON_VERSION}\\2#" pyrightconfig.json
  fi
fi

if [ -n "$PROJECT_NAME" ] && [ -f pyproject.toml ]; then
  PKG_NAME="${PROJECT_NAME//-/_}"
  sed -i.bak -E "s#^(name[[:space:]]*=[[:space:]]*\")my-[a-z-]+(\")#\\1${PROJECT_NAME}\\2#" pyproject.toml
  sed -i.bak -E "s#(packages[[:space:]]*=[[:space:]]*\\[\")src/my_[a-z_]+(\"\\])#\\1src/${PKG_NAME}\\2#" pyproject.toml
fi

if [ "$STRICT_TYPES" = "true" ] && [ -f pyrightconfig.json ]; then
  sed -i.bak -E "s#(\"typeCheckingMode\"[[:space:]]*:[[:space:]]*\")standard(\")#\\1strict\\2#" pyrightconfig.json
fi

find . -maxdepth 2 -type f -name "*.bak" -delete

echo
echo "[devenv-init] checking devenv.nix syntax..."
if nix-instantiate --parse devenv.nix >/dev/null 2>&1; then
  echo "[devenv-init] devenv.nix parse ok."
else
  echo "[devenv-init] WARN: devenv.nix failed to parse. Review the file." >&2
fi

cat <<'EOF'

Next steps:
  direnv allow            # or: devenv shell
  uv sync                 # install Python dependencies
EOF
