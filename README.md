# Cold Reader

A Claude Code marketplace with a single skill: **cold-reader**, which gives feedback on a
draft post, essay, article, or paper by reading it *in order, one chunk at a time* — the
way a real first-time reader experiences it, rather than seeing the whole thing at once.

Reading a document all at once (the default for an AI agent) hides pacing and structure
problems, because you already know how it ends. Reading strictly in order surfaces them:

- **Is it boring?** Where does interest sag?
- **Does it pay off its setups?** Does the post promise things it never delivers?
- **Can a busy reader tell what it's about, quickly?**
- **Where would a free reader stop?**

The framing comes from putanumonit's [chains or promises](https://putanumonit.com/2026/06/02/hw-03-chains-or-promises/):
a **captive reader** (a grader, a reviewer) finishes no matter what, while a **free reader**
keeps going only if each part earns the next. A good post is a *chain* that pulls the reader
forward; a bad one is a *promise* that keeps asking them to wait.

## How it works

1. The document is converted to Markdown (use `pandoc` for HTML/docx/PDF).
2. `chunk.py` splits it into numbered files — one per paragraph, heading, list item, table,
   code block, or image. Long paragraphs are split at sentence boundaries; short ones are
   kept whole. Images are extracted as their own files so a vision agent can see them (or,
   with `--no-vision`, replaced by their alt text as a screen reader would announce them).
3. A subagent reads the chunk files **one at a time, in order**, blind to what comes next,
   keeping notes on interest, confusion, questions, and promises — then writes a report.
4. You can read as one or more **personas** (free reader, paper reviewer, coworker, busy
   director, or a custom one), each producing its own report.

## Setup

Register this repo as a marketplace (one-time):

```bash
claude plugin marketplace add brendanlong/cold-reader-skill
```

Install the plugin:

```bash
claude plugin install cold-reader@cold-reader-skill
```

Restart Claude Code for the skill to take effect.

## Usage

Just ask, e.g.:

- "Cold-read my draft at ./post.md as a free reader."
- "Read this article the way a busy director would and tell me where they'd stop."
- "Cold-read this paper as a reviewer and check whether it pays off its claims."

## Updating

```bash
claude plugin marketplace update cold-reader-skill
claude plugin update cold-reader@cold-reader-skill
```

Restart Claude Code for changes to take effect. The update command checks the `version`
field in `.claude-plugin/marketplace.json`, so it must be bumped for updates to be detected.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — the chunker is a self-contained `uv` script that
  installs its own dependencies (`markdown-it-py`, `pysbd`, `requests`) on first run.
- `pandoc` (optional) — only needed to convert non-Markdown sources.

## Layout

```
plugins/cold-reader/
├── .claude-plugin/plugin.json
└── skills/cold-reader/
    ├── SKILL.md              # orchestration for the main agent
    ├── scripts/chunk.py      # Markdown → numbered chunk files
    └── personas/             # free-reader, paper-reviewer, coworker, busy-director
```
