# Before/after screenshots

For a PR that changes what a screen shows. The reviewer sees the change from the body, without
checking out the branch.

## Section

- review-first: `## 画面の before/after`, right after `## この PR で変わること`.
- show-me: the images go under `## Evidence`, as its Before and After.
- The repository's own template: its screenshot section when it has one; otherwise add the
  review-first heading, in the project's language, after the section that lists the changes.
  `check` accepts a heading the template does not list.

One subsection per screen, named by the screen and its path. Each holds the changed part cropped
and the whole screen, before and after:

```markdown
### 受注一覧（/orders）

変わった部分

![before: 変わった部分](/abs/scratch/orders-before-crop.png)
![after: 変わった部分](/abs/scratch/orders-after-crop.png)

画面全体

![before: 画面全体](/abs/scratch/orders-before-full.png)
![after: 画面全体](/abs/scratch/orders-after-full.png)
```

Keep the PNGs outside the checkout (a scratchpad directory) and write their absolute paths; wrap a
path that holds a space in `<>`. A screen that did not exist before has no before image; say so in
one line in its place.

## Capture

- Take each PNG on the real screen with headless Playwright, with the same data, viewport, and
  role for before and after. Claude in Chrome's `screenshot` saves a JPEG of the viewport, not a
  PNG.
- Before comes from the base branch or the environment that runs it, after from the PR head.
- Crop with `locator.screenshot({ path })` on the element that changed; take the whole screen with
  `page.screenshot({ path, fullPage: true })`. gh rejects an image over 10 MB.

## Attach

`gh` 2.99.0 or later uploads each `--attach` file to GitHub and rewrites every reference in the
body whose path resolves to it, so the images are never committed. It needs write access to the
repository.

```bash
gh pr create --body-file body.md \
  --attach /abs/scratch/orders-before-crop.png --attach /abs/scratch/orders-after-crop.png \
  --attach /abs/scratch/orders-before-full.png --attach /abs/scratch/orders-after-full.png
```

For an existing PR, run `gh pr edit <number> --body-file body.md` with the same `--attach` flags.
An attached file the body does not reference is appended to the end of the body, so a mistyped
path shows up there instead of in its section. An upload cannot be deleted; attach only the final
images.

After posting, read the body back with `gh pr view <number> --json body` and confirm that no local
path remains.
