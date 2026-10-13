# ast-grep Practice

Complement general-purpose lint tools (ESLint, oxlint, Biome, clippy, etc.) with ast-grep for patterns they cannot express. Always prefer reproducible static rules over natural-language prompts.

## Tool provenance and installation

Use an ast-grep binary installed from a trusted source, such as the workstation's
Nix or Homebrew packages. Locate and verify it outside the target repository,
record its absolute path, and use that path when inspecting an unfamiliar repo.
Check its version and help before selecting flags.

If the tool is missing, install it outside the target repository using trusted
package-manager configuration and verified package metadata/checksums. A repo's
`.npmrc` can replace the registry for `@ast-grep/cli`; disabling install scripts
alone does not make a substituted binary safe to run. Keep tool provisioning
outside that repo until its configuration is trusted.

Add a project-local dependency only when the project, its registry configuration,
and lifecycle-script policy are trusted and dependency changes are in scope.
Then follow `package.json`'s `packageManager`, use `@ast-grep/cli`, and use the same
package manager in CI. Commands below that use npm, npx, pnpm, or project binaries
assume this trusted-project condition; otherwise use the recorded trusted binary.

## Quick Start

Minimum configuration to verify operation:

```bash
mkdir -p rules rule-tests
cat > sgconfig.yml << 'EOF'
ruleDirs:
  - rules
testConfigs:
  - testDir: rule-tests
EOF

cat > rules/no-console-log.yml << 'EOF'
id: no-console-log
language: TypeScript
severity: warning
rule:
  pattern: console.log($$$ARGS)
message: Do not leave console.log behind.
fix: ''
EOF

cat > rule-tests/no-console-log-test.yml << 'EOF'
id: no-console-log
valid:
  - logger.info('ok')
invalid:
  - console.log('debug')
  - "console.log('a', 'b')"
EOF

ast-grep test --skip-snapshot-tests  # run tests
ast-grep scan src/                    # scan project
```

## Principles

- First check whether an existing linter can cover the rule
- Use ast-grep when you need "structural patterns"
- Develop rules with TDD: test-first → rule implementation → CI integration
- Write `fix` when you can. Prefer rules with auto-fix over detection-only rules

## Choosing between ast-grep and existing linters

| Case | Tool |
|------|------|
| unused import, no-console, naming convention | ESLint / oxlint / Biome |
| type error, unreachable code | TypeScript compiler / clippy |
| formatting | Prettier / Biome / rustfmt |
| Forbidding a specific function-call pattern | ast-grep |
| Detecting and rewriting deprecated APIs | ast-grep (fix) |
| Forbidden pattern inside a specific context | ast-grep (inside/has) |
| Project-specific structural constraints | ast-grep |

Signs that ast-grep is the right choice:
- The rule cannot be expressed by configuring existing rules
- It depends on parent/child/sibling AST relationships
- Automatic rewriting (migration) is required

## Project setup

### sgconfig.yml

```yaml
ruleDirs:            # required: directories holding rule files
  - rules
testConfigs:         # optional: test configuration
  - testDir: rule-tests
utilDirs:            # optional: shared utility rules
  - rule-utils
languageGlobs:       # optional: mapping for non-standard extensions (unnecessary for TS/JS/Python etc.)
  html: ['*.vue', '*.svelte', '*.astro']
```

### Directory layout

```
project/
  sgconfig.yml
  rules/
    no-direct-env-access.yml
    prefer-result-type.yml
  rule-tests/
    no-direct-env-access-test.yml
    prefer-result-type-test.yml
    __snapshots__/
  rule-utils/
    is-async-function.yml
```

`ast-grep scan` runs every rule under `ruleDirs` starting from the directory that contains `sgconfig.yml`.

## Rule file structure

```yaml
id: no-direct-env-access
language: TypeScript
severity: warning
rule:
  pattern: process.env.$KEY
  not:
    inside:
      kind: function_declaration
      has:
        pattern: getEnv
      stopBy: end
message: Do not reference process.env directly. Go through getEnv().
note: Ensures type-safe access to environment variables.
fix: getEnv('$KEY')
files:
  - "src/**"
ignores:
  - "src/config.ts"
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | rule identifier |
| `language` | Yes | target language |
| `rule` | Yes | match condition |
| `severity` | No | `hint`, `warning`, `error` |
| `message` | No | one-line description |
| `note` | No | detailed description / migration guide |
| `fix` | No | auto-fix template |
| `constraints` | No | additional constraints on metavariables |
| `transform` | No | text transformation of metavariables |
| `files` | No | target glob |
| `ignores` | No | excluded glob |
| `url` | No | documentation URL |

### Suppression comments

```typescript
// ast-grep-ignore
someCode()

// ast-grep-ignore: no-direct-env-access
process.env.NODE_ENV
```

## Metavariable pitfalls

Pattern-matching caveats:

- `$OBJ.$PROP` matches **dot access only**. It does not match `obj['key']` (bracket access)
- `$VAR` matches exactly one AST node
- `$$$VARS` matches zero or more AST nodes (variadic arguments, multiple statements, etc.)
- `$_` is a wildcard (does not capture). The same name can match different contents
- A metavariable must occupy a whole node: `obj.on$EVENT` and `"hello $NAME"` do not work

## fix (auto-fix)

### Deciding whether to attach a fix

`fix` is convenient, but since it is applied automatically it can change semantics. Do **not** attach one (detection-only) when:

- The rewrite changes type safety (e.g. `as any` → `as unknown` changes type-inference results)
- Side effects or evaluation order may change (short-circuit behavior, timing of exceptions)
- Context dependence means the correct replacement is not unique (e.g. API migrations that swap argument order; review is mandatory)
- The deletion entangles with other expressions in the same statement

When in doubt, skip `fix` and document the manual migration steps in `note`. Only attach `fix` when you are confident that "replacing everywhere is safe".

### Basics

```yaml
rule:
  pattern: console.log($ARG)
fix: logger.info($ARG)
```

Metavariables are usable as-is inside the `fix` template. Unmatched metavariables become empty strings.

### Deletion

```yaml
rule:
  pattern: console.log($$$ARGS)
fix: ''
```

`fix: ''` deletes the matched node. Note that a blank line may remain. **If you want to also remove trailing `;` on statements or trailing commas, always combine with `expandEnd`** (see "Range expansion" below). If leaving a blank line is acceptable, `expandEnd` is not needed. Rule of thumb: in projects where a formatter (Prettier etc.) runs after, blank lines get tidied automatically, so `expandEnd` is unnecessary; if no formatter runs, `expandEnd` is recommended.

### Fix when bundling multiple patterns with `any:`

If each branch under `any:` can use the **same fix template**, it is fine to consolidate into one rule:

```yaml
rule:
  any:
    - pattern: $ARR.filter($P).length === 0
    - pattern: $ARR.filter($P).length == 0
fix: '!$ARR.some($P)'   # metavariables shared across both branches + identical template
```

**If the fix differs per branch, always split into separate rules** (you cannot write a per-branch fix inside `any:`). Example: `=== 0` → `!some()` and `!== 0` → `some()` should be split. Splitting is acceptable even when the intent is the same (aligning the ids as `*-empty` / `*-nonempty` keeps things readable).

### Multi-line

```yaml
rule:
  pattern: |
    def foo($X):
      $$$S
fix: |-
  def bar($X):
    $$$S
```

Indentation is preserved relative to the original code's position.

### Range expansion (FixConfig)

When you want to include the trailing comma etc. in the deletion:

```yaml
fix:
  template: ''
  expandEnd:
    regex: ','
```

### Quick rewrites from the CLI

```bash
ast-grep run --pattern 'oldFunc($$$ARGS)' --rewrite 'newFunc($$$ARGS)' --lang typescript .
# --update-all applies everywhere without confirmation
```

## constraints

Add extra conditions to metavariables. Only `$ARG` is supported (`$$$ARGS` is not). It filters matches after the rule matches.

**Choosing between constraints and structural constraints (has/inside/not)**:
- You want to constrain **the contents of a metavariable** → `constraints` (e.g. `$METHOD` is one of `get` / `set` / `delete`)
- You want to constrain **the structure outside or inside the pattern** → `has` / `inside` / `not` / `precedes` / `follows` (e.g. inside a specific parent, or having a specific child)
- If you can write the concrete literal directly in the pattern, that is the simplest (`pattern: new Set($X)` guarantees Set is present)

Writing `pattern` together with `has` / `not` directly under `rule` is evaluated as **AND** (matches pattern AND has is true). Use this shape to tack on structural constraints that `pattern` alone cannot express.

```yaml
rule:
  pattern: $OBJ.$METHOD($$$ARGS)
constraints:
  METHOD:
    regex: '^(get|set|delete)$'
  OBJ:
    kind: identifier
```

Usable fields: `kind`, `regex`, `pattern`

Note: constrained metavariables inside `not` may not behave as expected.

## transform

Textually transform matched metavariables before using them in `fix`.

### replace (regex replacement)

```yaml
transform:
  NEW_NAME:
    replace:
      source: $NAME
      replace: 'get(\w+)'
      by: 'fetch$1'
fix: $NEW_NAME($$$ARGS)
```

### substring

```yaml
transform:
  INNER:
    substring:
      source: $STR
      startChar: 1
      endChar: -1
```

Negative indices count from the end. Same semantics as Python slicing.

### convert (case conversion)

```yaml
transform:
  SNAKE:
    convert:
      source: $NAME
      toCase: snakeCase
      separatedBy: [caseChange]
```

Supported cases: `camelCase`, `snakeCase`, `kebabCase`, `pascalCase`, `upperCase`, `lowerCase`, `capitalize`

### rewrite (experimental)

Recursively rewrite nodes inside a metavariable using rewriter rules.

```yaml
transform:
  REWRITTEN:
    rewrite:
      source: $$$BODY
      rewriters: [migrate-api-call]
      joinBy: "\n"
```

## utils (utility rules)

Reference shared rules defined under `utilDirs` with `matches`.

```yaml
# rule-utils/is-async-function.yml
id: is-async-function
language: TypeScript
rule:
  any:
    - kind: function_declaration
      has:
        field: async
        regex: async
    - kind: arrow_function
      has:
        field: async
        regex: async
```

```yaml
# rules/async-no-try-catch.yml
id: async-no-try-catch
language: TypeScript
rule:
  all:
    - matches: is-async-function
    - has:
        pattern: await $EXPR
        stopBy: end
    - not:
        has:
          kind: try_statement
          stopBy: end
message: async function lacks try-catch.
severity: warning
```

## Testing

There are two kinds of tests. Do not conflate them:
- **Classification test** (`test --skip-snapshot-tests`): only verifies that the code listed under `valid` / `invalid` is classified correctly. Use this during rule development before snapshots are ready.
- **Snapshot test** (`test` / `test -U`): pins the match positions and fix results on invalid code as snapshots and detects regressions. Generate locally with `-U`, review, and commit. Run unskipped `test` in CI to detect changed ranges and fixes.

### Test file format

The `id` inside the test file must match the `id` of the rule file. The filename is free (convention: `{rule-id}-test.yml`).

```yaml
# rule-tests/no-direct-env-access-test.yml
id: no-direct-env-access
valid:
  - getEnv('NODE_ENV')
  - "function setup() { return getEnv('PORT') }"
invalid:
  - process.env.NODE_ENV
  - process.env.PORT
```

### Running tests

```bash
# classification test (is valid/invalid correct?)
ast-grep test --skip-snapshot-tests

# generate / update snapshots
ast-grep test -U

# interactive snapshot review
ast-grep test --interactive
```

Test result markers:
- `.` : pass
- `N` : noisy (false positive — matches valid code)
- `M` : missing (false negative — fails to match invalid code)

### Workflow

1. Write the test file under `rule-tests/` (Red)
2. Write the rule under `rules/` (Green)
3. Verify with `ast-grep test --skip-snapshot-tests`
4. Generate snapshots with `ast-grep test -U`
5. Review the snapshots, run unskipped `ast-grep test`, and commit

## CI integration

### justfile

```just
ast-grep-test:
  ast-grep test

ast-grep-lint:
  ast-grep scan

check: format-check typecheck ast-grep-lint test
```

### GitHub Actions

Align tools with the dev environment (use pnpm in CI if the project uses pnpm, npm if it uses npm):

```yaml
- uses: actions/setup-node@v7
  with: { node-version: 24, cache: npm }   # for pnpm projects: pnpm/action-setup@v6 + cache: pnpm

- run: npm ci   # for pnpm: pnpm install --frozen-lockfile

- name: ast-grep rule tests
  run: npx ast-grep test

- name: ast-grep scan
  run: npx ast-grep scan --error
```

**severity and exit codes**:
- `ast-grep scan` exits non-zero by default if at least one finding has `error` severity
- Passing `--error` makes `warning` / `hint` also cause non-zero exit (use when CI should fail on warnings too)
- `--error=my-rule` promotes only the named rule ID to error; its value is not a severity threshold
- `--json` for structured output (for integrating with other tools)

## Looking up kind names

Kind names depend on the language's Tree-sitter grammar.

```bash
# AST dump (named nodes only, use these when writing rules)
ast-grep run --pattern 'YOUR_CODE' --lang typescript --debug-query=ast

# CST dump (all nodes, including anonymous tokens)
ast-grep run --pattern 'YOUR_CODE' --lang typescript --debug-query=cst
```

See [references/kind-catalog.md](kind-catalog.md) for a per-language catalog of common kinds (covers TypeScript / Rust / Go / Python).

## Practical rule examples

### TypeScript: forbid `as any` casts (detection only, no fix)

```yaml
id: no-as-any
language: TypeScript
severity: error
rule:
  pattern: $EXPR as any
message: as any disables the type system. Go through as unknown or a type guard.
note: |
  Why no auto-fix: mechanically replacing `as any` → `as unknown` changes type
  inference results and introduces new compile errors at call sites. Detection-only,
  migrate manually.
```

When matching a type assertion like `as any`, `$EXPR as any` works on the `as_expression` node. `$EXPR` matches the whole left-hand side, so it matches both `JSON.parse(raw) as any` and `(value as any)`.

### TypeScript: rewrite a deprecated API

```yaml
id: migrate-old-api
language: TypeScript
severity: error
rule:
  pattern: oldClient.fetch($URL, $OPTS)
fix: newClient.request($URL, $OPTS)
message: oldClient.fetch is deprecated. Migrate to newClient.request.
```

### Forbid a specific import

See the tested rule [no-lodash-import](../assets/examples/rules/no-lodash-import.yml) and its
[valid/invalid fixtures](../assets/examples/rule-tests/no-lodash-import-test.yml).
The examples are checked in CI with `ast-grep test --config assets/examples/sgconfig.yml`.

### TypeScript: forbid direct fetch inside React components

```yaml
id: no-fetch-in-component
language: TypeScript
severity: warning
rule:
  pattern: fetch($$$ARGS)
  inside:
    any:
      - kind: function_declaration
        has:
          field: return_type
          pattern: JSX.Element
      - kind: arrow_function
        inside:
          kind: variable_declarator
          regex: '^[A-Z]'
    stopBy: end
message: Do not fetch directly inside a component. Use hooks or a server action.
```

### Rust: forbid unwrap()

```yaml
id: no-unwrap
language: Rust
severity: warning
rule:
  pattern: $EXPR.unwrap()
  not:
    inside:
      kind: function_item
      regex: '#\[test\]'
      stopBy: end
message: Do not use unwrap() outside tests. Use ? or expect().
note: unwrap() panics, so avoid it in production code.
```

### Rust: flag unsafe blocks

```yaml
id: flag-unsafe-block
language: Rust
severity: warning
rule:
  kind: unsafe_block
message: unsafe block. Explain the safety rationale in a comment.
```

### Rust: migrate println! to log macros

```yaml
id: no-println-in-lib
language: Rust
severity: warning
rule:
  pattern: println!($$$ARGS)
  not:
    inside:
      kind: function_item
      regex: 'fn main'
      stopBy: end
message: Do not use println! in library code. Use log::info! etc.
fix: log::info!($$$ARGS)
files:
  - "src/lib.rs"
  - "src/**/mod.rs"
  - "src/**/*.rs"
ignores:
  - "src/main.rs"
  - "src/bin/**"
```

### Go: detect ignored errors

See the tested rule [no-ignored-error](../assets/examples/rules/no-ignored-error.yml) and its
[valid/invalid fixtures](../assets/examples/rule-tests/no-ignored-error-test.yml).
The examples are checked in CI with `ast-grep test --config assets/examples/sgconfig.yml`.

### Go: prevent forgetting defer Close()

See the tested rule [defer-close-after-open](../assets/examples/rules/defer-close-after-open.yml) and its
[valid/invalid fixtures](../assets/examples/rule-tests/defer-close-after-open-test.yml).
The examples are checked in CI with `ast-grep test --config assets/examples/sgconfig.yml`.

### Python: forbid bare except

```yaml
id: no-bare-except
language: Python
severity: warning
rule:
  kind: except_clause
  not:
    has:
      kind: identifier
      stopBy: neighbor
message: Do not use bare except. Specify the exception type.
```

### Python: migrate print() to a logger

```yaml
id: no-print-in-src
language: Python
severity: warning
rule:
  pattern: print($$$ARGS)
  not:
    inside:
      kind: function_definition
      regex: 'def main'
      stopBy: end
message: Use logger instead of print().
fix: logger.info($$$ARGS)
files:
  - "src/**"
```

## References

### In-skill details

- [references/rule-yaml.md](rule-yaml.md) — all rule YAML fields, evaluation order, metavariable binding scope, `any:` + fix consolidation/splitting, `$$$ARGS` empty match, etc.
- [references/testing.md](testing.md) — classification vs snapshot tests, multi-line code notation, snapshot operation
- [references/cli.md](cli.md) — subcommands and flags in general, `--error` / exit codes / `--json`
- [references/kind-catalog.md](kind-catalog.md) — per-language kind catalog (TypeScript / Rust / Go / Python)

### Official

- ast-grep docs: https://ast-grep.github.io/
- Rule reference: https://ast-grep.github.io/reference/yaml.html
- sgconfig: https://ast-grep.github.io/reference/sgconfig.html
- Playground: https://ast-grep.github.io/playground.html
- Rule catalog: https://ast-grep.github.io/catalog/
