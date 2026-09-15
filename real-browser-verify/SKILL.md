---
name: real-browser-verify
description: Verify changed UI behavior in a real authenticated browser and capture evidence. Use for frontend changes, UI bug fixes, or deployment claims.
---

# Real-Browser Verify

The bar for "verified" on changed UI behavior is to reach the actual target
screen in a real browser, authenticate as the affected role, and exercise the
functional path. A passing suite or loaded page proves a different claim.

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

## Tool boundary

Use the real-browser capability available in the current harness. In a context
without browser access, report this verification as blocked and state the weaker
check separately.
