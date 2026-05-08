# Rust templates (planned)

Not yet implemented. Tracked for: `rust` (bare).

When added, expected toggles:
- `features.rust.channel` enum: `stable | beta | nightly`
- `features.cargo-watch.enable`

Until then, hand-write devenv.nix using `languages.rust.enable` + `channel` from devenv docs.
