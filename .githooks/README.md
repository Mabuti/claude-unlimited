# Git hooks: keep personal data and credentials out of commits

Two hooks live here:

- **`commit-msg`** -- scans the commit message you're about to write for
  email addresses. This is the important one: a commit *message* is never
  covered by a diff review, and that's exactly how this fork leaked real
  addresses before (see below).
- **`pre-commit`** -- scans the staged diff (the code you're about to
  commit) for the same kind of email address, plus obvious credential
  shapes (API keys, GitHub tokens, Slack tokens, PEM private-key headers).
  This is the secondary net, for the diff rather than the message.

Both enforce the same rule CONTRIBUTING.md already states: *"Never commit
credentials, tokens, or personal data -- including in tests, comments, and
screenshots."*

Both are Python 3 standard library only -- no third-party imports, no venv.
They run in well under a second on this repo.

## Install

Git does not use this directory by default -- point your local checkout at
it once:

```
git config core.hooksPath .githooks
```

That's a per-clone setting (it lives in your own `.git/config`, not in
history), so every contributor runs it once after cloning.

## Why this exists

On 2026-09-22 it was confirmed that two commits in this fork pasted real
terminal output into their commit message *bodies*, and that output
contained the maintainer's real work email addresses. Both commits are
reachable from the already-published tags `v1.2.7.3` and `v1.2.7.4`, and
per `docs/RELEASING.md` a published tag is never moved -- so that exposure
is permanent and these hooks exist so it can't happen a third time. (The
addresses themselves aren't reproduced here or anywhere in this guard; the
point is the mechanism, not the specific leak.)

The shape of the failure matters: the leak was in the commit *message*, not
the diff. A `pre-commit` hook never sees the message at all -- that's why
`commit-msg` is the essential hook here and `pre-commit` is the
supplementary one for the code itself.

## What counts as an email address

The matcher is deliberately conservative, because the first version of these
hooks blocked this repo's own test files (`python@3.14` in a Homebrew path,
`a@b.com` in a JWT fixture, `eslint@8.2.0`, `logo@2x.png`). **A hook that
blocks legitimate work gets turned off, and a hook that is turned off guards
nothing** -- a false positive is not a cosmetic bug in a guard like this, it
is the failure mode that disables it.

A string is treated as an address only when all of these hold:

| Rule | What it kills |
|---|---|
| Local part is **2+ characters** | `a@b.com` |
| First domain label is **2+ characters** | `a@b.com`, `pkg@1.2.3` |
| TLD is **alphabetic, 2-24 characters** | `python@3.14`, `eslint@8.2.0`, `node@18.17.1` |
| TLD is **not a known file extension** | `logo@2x.png`, `icon@3x.png`, `bundle@main.js` |
| There is a **local part** before the `@` | `@media`, `@scope/name`, `@pytest.mark.parametrize` |
| The domain is **dotted** (or literally `localhost`) | `image@sha256:abc...`, `foo@bar` |

The file-extension list is `NON_TLD_SUFFIXES` at the top of each hook.

Text is **NFKC-normalised before matching**, so a fullwidth `＠` (U+FF20) and
other compatibility forms fold to their ASCII equivalents and cannot walk
past the matcher.

## The dewrap pass (`commit-msg` only)

The incident these hooks exist to prevent was *pasted terminal output* -- and
pasted output wraps. An address broken by a hard wrap looks like

```
contact nobody@
acme-corp.com for details.
```

and neither half matches on its own. So `commit-msg` scans twice:

1. line by line, as before;
2. again over a **dewrapped** copy of the message.

Dewrapping removes a newline **only** when the character immediately before
it and the character immediately after it are both non-whitespace -- that is
what a hard wrap looks like. Every other newline is kept. Stripping newlines
unconditionally would *invent* addresses that were never in the message (a
line ending `...contact` followed by a line starting `support@...`), which
is a worse failure than the one being fixed.

A hit found only in the second pass is reported as
`non-allowlisted address SPLIT ACROSS LINES n-m`, with both line numbers, so
you know why grepping one line won't find it.

## Masking: the hook never prints a credential

`pre-commit` reports a credential hit as its identifying prefix plus a
length, never its body:

```
  s.py:1 [Anthropic API key (sk-ant-...)]
    -> sk-ant-...<redacted, 49 chars>
```

A guard whose job is to stop a secret leaking must not itself copy that
secret into shell scrollback or -- worse -- a CI job log, where it is
retained and readable by everyone with access to the build.

**Email addresses are the deliberate exception: they are printed verbatim.**
An address is not a credential, the author needs to know *which* of several
to fix, and it is a string they just typed. That asymmetry is intentional
and is commented as such in the source.

## Allowlist

Both hooks allow the same set of addresses, since they're not personal
data:

- anything `@example.com`, `@example.org`, `@example.net`, or
  `@example.test` (the addresses reserved for documentation/examples)
- anything `@localhost`
- `noreply@github.com`
- any `*@users.noreply.github.com` address (GitHub's own commit-identity
  addresses)

To add an entry, edit the `ALLOWLISTED_DOMAINS` / `ALLOWLISTED_ADDRESSES`
set at the top of `commit-msg` and the matching set in `pre-commit` (they're
independent, standalone scripts on purpose -- update both). Only add a
domain that is genuinely safe to publish; this list is meant to stay short.

`NON_TLD_SUFFIXES` and `EMAIL_RE` are duplicated the same way, for the same
reason, and must likewise be kept in step.

## Known limits

- **The executable bit has to be recorded in git, not just set on disk.** This
  repo has `core.fileMode = false`, because the primary checkout sits on a
  Windows-mounted `/mnt/d` path where the bit is not reliable. With that set,
  `git add` records mode 100644 no matter what `ls -l` says, and git then
  IGNORES a non-executable hook — silently, with only an `advice.ignoredHook`
  line to show for it. Both hooks are committed as 100755 via
  `git update-index --chmod=+x`. If you ever recreate them, check
  `git ls-tree HEAD .githooks/` rather than `ls -l`, or the guard will look
  installed and do nothing.


These are accepted gaps, not oversights. Each one is a trade made to keep
the hooks from crying wolf -- see the reasoning above.

- **Cyrillic and other homoglyph domains are not detected.** NFKC folds
  fullwidth and compatibility forms, but `асme-corp.com` with a Cyrillic
  `а` (U+0430) is a genuinely different string and this guard will not
  flag it. Chasing script-mixing detection would add a confusables table
  and a new class of false positive for no realistic gain: the threat model
  here is an accidental paste, not an adversary evading the hook. Someone
  deliberately smuggling an address past the hook already has `--no-verify`.
- **Single-character local parts and single-character first domain labels
  are not flagged.** `j@acme-corp.com` and `someone@x.co` slip through.
  This is what makes the repo's own `a@b.com` JWT fixture committable. A
  real personal address that short is vanishingly rare, and the cost of
  missing one is far below the cost of the hooks being disabled.
- **A handful of real ccTLDs are shadowed by the file-extension list** --
  notably `.sh`, `.md`, `.py`, `.rs` and `.ts`. An address at one of those
  will not be flagged. They collide with extensions that appear constantly
  in this repo's diffs, and the extension reading is overwhelmingly the
  likelier one.
- **IP-literal addresses (`user@10.0.0.5`) are not flagged**, because the
  TLD must be alphabetic.
- **`pre-commit` only scans added lines** in the staged diff. It does not
  scan file *names*, binary blobs, or anything a commit removes.
- **`commit-msg` scans the message only.** Neither hook can see history, so
  neither can catch something that already landed.

## `--no-verify`

`git commit --no-verify` skips both hooks. That's a real escape hatch, not
a bug -- there will be legitimate cases (a false positive, a deliberately
public example address the allowlist doesn't cover yet). Using it is a
**decision**: the person running it is personally vouching that what they're
about to commit doesn't contain credentials or personal data, in the same
way `--no-verify` always means "I checked, trust me" rather than "make the
check go away." It is not a routine workaround for a hook that's in your
way -- if the hooks are wrong often enough that you reach for `--no-verify`
regularly, fix the hook (or the allowlist) instead.
