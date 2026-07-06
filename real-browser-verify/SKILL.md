---
name: real-browser-verify
description: 'Verify a UI change or bug fix by driving the real running app through a real (authenticated) browser — dev-server lifecycle, target-screen navigation, functional operation, and evidence capture with guaranteed cleanup. Use before declaring any UI change, frontend bug fix, or deploy "done"; Playwright/E2E specs, curl, page-loads, or console-error counts are NOT acceptable substitutes for this check. Trigger phrases: 画面で確認, 実ブラウザで検証, 動作確認して, browser use で確認.'
---

# Real-Browser Verify

The bar for "verified" on UI-affecting work: **reach the actual target screen in
a real browser, authenticated, and exercise the functional path the change
affects.** A passing test suite, a loaded page, or a clean console is evidence of
something else. (Measured across ~2,000 sessions: false completion was the #1
correction, and "real browser operation" was the #1 re-typed instruction.)

## Procedure

1. **Server lifecycle (start-if-absent, never duplicate)**
   - Check if the dev server is already up (`curl -sI http://localhost:<port>` /
     health endpoint). Start it only if absent, in the background with output to
     a log file.
   - Register cleanup at start (`trap` in the launching script) so the server and
     any browser session are torn down even on failure — the hand-rolled
     start→check→kill ritual hangs regularly; make teardown unconditional.
   - Use the project's own port (not a default 3000/5173/8080/8000 assumption);
     read it from the repo config.

2. **Authenticate for real**
   - Log in as the role the change affects. A login-redirect page that returned
     HTTP 200 is NOT the target screen — verify you are past auth by checking
     for an element only the authenticated screen has.
   - An unauthenticated `curl` 401 proves nothing about the feature.

3. **Reach the actual target screen and operate it**
   - Navigate to the specific screen the change affects, not the top page.
   - Exercise the functional path: click the button, submit the form, run the
     search — reproduce the originally-reported symptom's scenario and confirm it
     no longer occurs. Screen *reachability* and *functionality* are two separate
     axes; check both.
   - For persistence-related fixes, test the with-reload and without-reload paths
     as separate scenarios.
   - Confirm the real path is live, not a mock/fallback (real LLM key, real
     websocket connection) — a deterministic fallback answering is a failed
     verification.

4. **Capture evidence**
   - Screenshot of the target screen post-operation (light/dark/mobile when the
     change is visual).
   - Console errors and relevant network failures captured, not just eyeballed.
   - Completion reports must include the evidence; a narrative "verified working"
     without artifacts does not count.

5. **Cleanup**
   - Kill only what you started. Leave pre-existing servers running.
   - Close billed sessions/resources (avatar/realtime connections) explicitly.

## Tool mapping

- Claude Code: `claude-in-chrome` MCP tools (or the project's `run` skill).
- Codex CLI: the embedded browser / `$browser:control-in-app-browser` /
  `$chrome:control-chrome` facilities.
- CI contexts with no browser: say so explicitly and report the verification as
  BLOCKED with the next-best check performed — do not silently substitute a
  weaker check and call it verified.
