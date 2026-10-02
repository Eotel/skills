# One page for many answers

Build it as an Artifact (load the artifact design guidance first, and the
capabilities guidance for the `db` part).

- One ticket per session: the session's plain-language name, its worktree path,
  how long it has waited, one or two sentences on what happened, then its
  questions. Large simple pictures over prose when the user asks for an
  explain-like-I'm-five page.
- Each question: option cards with the recommended one tagged and first, plus a
  free-text memo. A "fill the rest with recommendations" button.
- Grill pages a session already published: copy their questions and options
  verbatim and link the original. Images they use can be fetched with the
  Artifact tool's `read` (`paths`) and republished as the new page's `files`.
- A cleanup section: one close/keep row per `finished` or `unstarted` worktree,
  with what `rm_check.py` found and anything the row's choice also removes, and
  one row per kept worktree with `closable` tabs, naming each tab.
- Answers: declare `db` with an owner-only root rule, write one document
  (`answers/latest`) on submit, and also offer "copy as text" for pasting into
  the chat. Read the document back with `ArtifactData get`, or take the pasted
  text; the user may use either.
- For a second round, republish the same file path with a new localStorage key so
  earlier picks do not reappear.
