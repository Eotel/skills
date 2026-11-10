# Desktop app checks (macOS)

For a change that runs in a desktop app built around a web view (Tauri, wry, another WKWebView
shell). `scripts/desktop_app.py` loads two helpers into the app: one keeps it behind the user's
windows, the other takes commands from the script inside the app's own process. The check needs no
screen-recording, accessibility, or input-posting rights, and the user's front window and pointer
stay theirs.

## Run

1. Build a debug binary with the frontend inside it (for Tauri, a debug build without bundling;
   check the flags with `tauri build --help`). Launch the executable, not the `.app` folder.
   A release build signed with the hardened runtime ignores the helpers.
2. Point the app at throwaway data through its own setting (for example a data folder variable),
   so the check never writes to the user's real files.
3. Launch it. The script compiles the helpers with `clang` when they are missing or older than
   their sources, starts the binary, and returns once the app's page has finished loading:

   ```bash
   real-browser-verify/scripts/desktop_app.py launch <binary> --env DATA_DIR=<scratch>
   ```

   It prints JSON with `pid`, the helper log, and the app's own output log.
4. Operate and read it with `send <pid>`, one command per call; each prints the app's answer:
   - `key cmd+z`, `key r`, `key escape`, `key shift+tab`: one key press.
   - `text 'abc'`: one key press per character (no IME).
   - `click <x> <y>` (`--count 2` for a double click): page point in CSS pixels, the same
     numbers as `getBoundingClientRect()`.
   - `shot <path>`: the web view as a PNG, at the screen's scale.
   - `js '<expression>'`: the expression's value, for reading state (`document.title`,
     an element's text, a button's `disabled`).
5. `stop <pid>` ends the app. `stop` only ends apps this script launched.

Operate through `key`, `text`, and `click`, so the app's own handlers run as they do for a person;
use `js` to read state and to find coordinates. Clicking where nothing is shown, or a script that
changes state, proves less than the check claims.

## Limits

- Japanese and other IME input cannot be checked behind the user's windows: the input source is
  one for the whole Mac. Ask before borrowing the foreground for it.
- Clicks reach the web view only. Native menus, dialogs, and the title bar stay out of reach.
- A shortcut that the app's native menu owns (such as ⌘A for Select All) goes to the menu first.
- `launch` starts the binary directly. A wrapper such as `nohup` or `/bin/sh` is protected by
  macOS and drops the helpers; the app then opens in front of the user's windows.
