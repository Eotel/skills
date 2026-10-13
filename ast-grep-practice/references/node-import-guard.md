# Node --import guard

When spawning Node with a filesystem path in `--import`, convert that path with
`pathToFileURL(path).href`. Node resolves import specifiers as URLs, so `#`, `?`,
and Windows drive letters can otherwise change the meaning of a native path.
See [Node ESM URLs](https://nodejs.org/api/esm.html#urls).

The shared preset is [../assets/node-import/sgconfig.yml](../assets/node-import/sgconfig.yml).
Run from the skill root:

```bash
ast-grep test --config assets/node-import/sgconfig.yml --skip-snapshot-tests
ast-grep test --config assets/node-import/sgconfig.yml
ast-grep scan --config assets/node-import/sgconfig.yml /path/to/repository
```

Installing the skill does not activate the preset in a repository's CI. Add its
rules to that repository's ruleDirs, or pass the preset config explicitly.
A user-wide `~/.config/ast-grep` can link its config, rules, and rule-tests to this
preset so it remains versioned through the skill's APM source.

## Scope

JavaScript and TypeScript rules flag direct join/resolve expressions and const
bindings in the same nearest block passed to Node's --import. They assume those
function and spawn names denote Node APIs; inspect a warning if names are bound
to unrelated functions. Outer bindings, helper returns, aliased imports, and
`--import=value` are not inspected.

Keep the warnings detection-only: adding pathToFileURL requires an import, and
relative paths must first be resolved against the child's intended directory.
The fixtures cover safe file URLs, package names, require paths, and shadowed
names, as well as the failing preload pattern.
