#!/usr/bin/env bash
# spawn-with-goal.sh — spawn a named agmsg agent (codex | claude-code) AND give it
# a goal in the SAME first turn, so it acts immediately without needing a Monitor.
#
# WHY THIS EXISTS
#   agmsg `spawn` boots the agent with only `/<cmd> actas <name>`; the agent
#   registers, then goes idle. A *codex* peer has no Monitor (no real-time push
#   delivery — see agmsg's docs/codex-monitor-beta.md), so a goal you `send` AFTER
#   spawn is never noticed. This wrapper instead (1) pre-sends the goal onto the
#   bus and (2) boots the agent with an actionable first-turn prompt: register →
#   read inbox → do the latest task → reply to its sender. One turn, no monitor,
#   no PATH shim, no lingering bridge.
#
#   This is the zero-infra "strategy A". If your agmsg `spawn` gains a native
#   --prompt option (proposed upstream), prefer that. For ongoing two-way chat
#   with a long-lived codex peer, prefer codex-monitor-beta instead.
#
# REQUIRES tmux (opens a new window). Types: codex | claude-code.
#
# USAGE
#   spawn-with-goal.sh <type> <name> --team <team> [--project <path>] \
#                      [--from <orchestrator-name>] -- <goal text...>
#
#   <type>     codex | claude-code
#   <name>     actas identity for the spawned worker
#   --team     team both the worker and the orchestrator live on (required)
#   --project  dir to launch in / register under (default: $PWD)
#   --from     orchestrator identity that sends the goal and receives the reply
#              (default: orchestrator)
#   --         everything after this is the goal text handed to the worker
#
# ENV
#   AGMSG_SCRIPTS  override path to agmsg's scripts/ dir
#                  (default: ~/.agents/skills/agmsg/scripts)
set -euo pipefail

AGMSG="${AGMSG_SCRIPTS:-$HOME/.agents/skills/agmsg/scripts}"
die() { echo "spawn-with-goal: $*" >&2; exit 1; }

[ -d "$AGMSG" ] || die "agmsg scripts not found at $AGMSG (set AGMSG_SCRIPTS)"
[ -n "${TMUX:-}" ] || die "must run inside tmux (the worker opens in a new window)"

TYPE="${1:-}"; NAME="${2:-}"
[ -n "$TYPE" ] && [ -n "$NAME" ] || die "usage: spawn-with-goal.sh <type> <name> --team <team> [opts] -- <goal>"
shift 2

TEAM=""; PROJECT="$PWD"; FROM="orchestrator"; GOAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --team)    TEAM="${2:?--team needs a value}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a path}"; shift 2 ;;
    --from)    FROM="${2:?--from needs a value}"; shift 2 ;;
    --)        shift; GOAL="$*"; break ;;
    *)         die "unknown arg: $1 (did you forget '--' before the goal text?)" ;;
  esac
done

[ -n "$TEAM" ] || die "--team is required"
[ -n "$GOAL" ] || die "goal text is required after '--'"
case "$TYPE" in codex|claude-code) ;; *) die "type must be 'codex' or 'claude-code'" ;; esac

# Installed command name (agmsg, or a custom name from `install.sh --cmd`).
CMD="$(basename "$(cd "$AGMSG/.." && pwd)")"
# CLI binary differs from the agmsg type label for claude-code.
CLI="$TYPE"; [ "$TYPE" = claude-code ] && CLI="claude"
command -v "$CLI" >/dev/null 2>&1 || die "$CLI not found on PATH"

# 1. Make sure the orchestrator identity exists so it can receive the reply,
#    and pre-join the worker so its actas just claims (no interactive team prompt).
"$AGMSG/join.sh" "$TEAM" "$FROM" claude-code "$PROJECT" >/dev/null 2>&1 || true
"$AGMSG/join.sh" "$TEAM" "$NAME" "$TYPE"       "$PROJECT" >/dev/null 2>&1 || true

# 2. Put the goal on the bus, addressed to the worker.
"$AGMSG/send.sh" "$TEAM" "$FROM" "$NAME" "$GOAL"

# 3. Boot the worker with an actionable first-turn prompt. The leading slash
#    command is the reliable trigger that loads the agmsg skill and claims the
#    identity (verified); the trailing instruction tells it to consume its inbox
#    in that same turn — which is what an idle, monitor-less codex would otherwise
#    never do.
BOOT="/${CMD} actas ${NAME}

Then, still acting as ${NAME}: run /${CMD} to read your inbox, treat the most
recent message as your task, carry it out, and send the result back to its
sender using /${CMD} send. Do not modify files unless the task requires it."

# new-window keeps the worker off the orchestrator's current pane.
tmux new-window -n "$NAME" \
  "cd $(printf '%q' "$PROJECT"); $(printf '%q' "$CLI") $(printf '%q' "$BOOT"); exec \${SHELL:-/bin/bash} -i"

echo "spawned $TYPE '$NAME' on team '$TEAM' with a goal; reply will come back to '$FROM'."
echo "watch:  $AGMSG/history.sh $TEAM"
echo "inbox:  $AGMSG/inbox.sh $TEAM $FROM"
# Teardown note: this wrapper launches via `tmux new-window`, not agmsg `spawn`,
# so there is no placement record for `despawn` to act on. Tear down by closing
# the window and deregistering the identity directly:
echo "stop:   tmux kill-window -t '$NAME'  &&  $AGMSG/leave.sh $TEAM $NAME"
