---
name: cold-reader
description: Use when the user wants feedback on a draft post, article, essay, or paper by having it read in order like a first-time reader — to check pacing, whether it's boring, whether it sets up things it doesn't pay off, and whether a busy reader can quickly tell what it's about. Reads the document one chunk at a time (a "cold read") through a persona, then reports.
allowed-tools: ["Bash", "Task", "Read", "Glob", "Write", "TodoWrite"]
---

# Cold Reader

Give feedback on a document by reading it **in order, one chunk at a time**, the way a
real first-time reader experiences it — instead of seeing the whole thing at once (which
is how you'd normally read it, and which hides pacing and setup/payoff problems).

## The idea

A reader is either **captive** or **free** (from putanumonit's "chains or promises"):

- A **captive reader** (a grader, a paper reviewer) finishes no matter what.
- A **free reader** (someone who found your blog post in a feed) keeps reading only as
  long as *each part earns the next*. A good post is a **chain** that pulls the reader
  forward; a bad one is a **promise** that keeps asking them to wait for the good part.

Reading the whole document at once makes everything look connected because you already
know how it ends. The only way to catch "this is boring here," "this sets up something
it never delivers," or "I couldn't tell what this was about" is to read strictly in
order, committing to a reaction to each chunk *before* seeing the next. That's what this
skill does: a subagent reads numbered chunk files one at a time, keeps notes, and reports.

## Workflow

### 1. Get the document into Markdown

The chunker takes Markdown. If the source is HTML, docx, PDF, etc., convert it first —
`pandoc input.html -t gfm -o /tmp/source.md` handles most formats. Save the Markdown
somewhere like `/tmp/source.md`. Keep the file next to its images if they're local, since
image paths resolve relative to it.

**If the user gave you a URL**, prefer a readability-style extractor over converting the
raw page — a full-page `pandoc` keeps the nav, sidebar, and footer as chunks, which
unfairly pollutes the cold read. A tool like `trafilatura` (`trafilatura -u URL --markdown
--images > /tmp/source.md`, or the Python API with `output_format="markdown"`) strips the
boilerplate, extracts the title, and rewrites image URLs to absolute in one step. No
extractor is perfect across CMS themes, so **eyeball the result before chunking**: check
that headings survived as headings (some themes self-link headings, which extractors can
flatten into plain links) and drop any print-only footnote/URL dumps. Then:

- If images are still **page-relative** (e.g. `images/foo.png`, common when you used
  `pandoc` on a downloaded page), pass `--base-url "<the page URL>"` to the chunker so it
  resolves and downloads them instead of failing to alt text. This matters most for
  image-heavy posts, where losing the figures guts the read.
- If the extractor gave you a body without the title, supply it with `--title` (step 3).

### 2. Choose persona(s)

Ask the user which persona(s) to read as if they haven't said. Available personas live in
`${CLAUDE_PLUGIN_ROOT}/skills/cold-reader/personas/`:

- `free-reader` — blog / Reddit / LessWrong reader with a short attention span (default for personal posts)
- `paper-reviewer` — workshop / research paper reviewer
- `coworker` — interested technical peer
- `busy-director` — senior leader skimming for impact / the ask

You can run several personas; each gets its own subagent and its own report. The user may
also describe a custom persona — write it to a temp `.md` file in the same format and use that.

### 3. Chunk the document

Pick a fresh working directory and run the chunker (the shebang runs it via `uv`, which
installs its own dependencies — no setup needed):

```bash
WORKDIR=$(mktemp -d /tmp/cold-reader-XXXXXX)
"${CLAUDE_PLUGIN_ROOT}/skills/cold-reader/scripts/chunk.py" /tmp/source.md --workdir "$WORKDIR" --title "The Document's Real Title"
```

Flags:
- `--title "..."` — the document's title, emitted as the first chunk. **Pass this** unless
  the title is already the first line of the Markdown. A real reader sees the title before
  the body — it's often what tells them what the piece is about and bridges the opening — so
  omitting it makes the cold read unfairly harsh on the intro. Many exports (e.g. the
  LessWrong API `markdown` field) give you the body without the title; supply it here.
- `--base-url "..."` — the original page URL. Page-relative image srcs (e.g.
  `images/foo.png` from a fetched HTML page) are resolved against it and downloaded, instead
  of being looked up as local files and degrading to alt text. Pass this whenever the
  Markdown came from a URL and still has relative image paths.
- `--no-vision` — replace images with `Image: <alt text>` (how a screen-reader user
  experiences them). Use this to evaluate accessibility, or if the reading agent has no vision.
- `--target-words N` — target words per chunk (default 90). Sentences and list items are
  accumulated up to this size, then flushed: short paragraphs and list bullets merge into a
  natural reading unit instead of one tiny chunk each, and a long paragraph splits at sentence
  boundaries near the target. Headings, images, code, and tables are always hard breaks.
- `--min-words N` — floor below which a chunk keeps accumulating across a paragraph/list
  boundary rather than flushing (default: half of `--target-words`), so a chunk isn't emitted
  far below target just because a block ended.

If it exits 0, the setup is valid — there is no separate validation step. The script
prints the chunk count and image count; note them.

### 4. Spawn a reader subagent per persona

For each persona, launch a subagent with the Task tool (`subagent_type: "general-purpose"`).

**Critical for a valid cold read:** the subagent must be *blind* to everything except the
chunk files. Do **not** paste the document text, a summary, the title, or your own opinion
of it into the prompt. Give it only the workdir path and the persona. The whole point is
that it discovers the document in order, the way a real reader would.

Use this prompt template, filling in the two placeholders:

```
You are doing a "cold read" of a document to give the author feedback. You will read it
strictly in order, one chunk at a time, reacting to each chunk BEFORE you see the next —
exactly as a real first-time reader experiences it. You cannot see ahead, and that is the
point. Do not try to find or open the original document; read ONLY the chunk files.

## Your persona
<PASTE THE FULL CONTENTS OF THE PERSONA FILE HERE>

Read and inhabit this persona. React as this reader would — their patience, their goals,
their reasons to keep reading or to stop.

## The chunks
The document has been split into numbered files in this directory:

    <WORKDIR>

List that directory to see the range (e.g. chunk-001.md through chunk-041.md). Files are
numbered in reading order. Most are .md text; some are images (.png/.jpg) — Read those to
see the actual image, reacting as your persona would to a picture at that point. A chunk
that just says "Image: ..." is alt text (the document was read without vision).

Seeing the total number of files is fine (a real reader sees a scrollbar too), but you
must still read them one at a time and commit to your reaction to each before opening the next.

## How to read
Go through the chunks in numeric order. After reading each chunk, and before reading the
next, jot down notes for that chunk number:

- **Interest (1-5):** how engaged you are right now, as this persona.
- **What you now understand:** what you think the document is about / is claiming so far.
- **Confusion:** anything unclear, undefined, or referenced-but-not-explained.
- **Questions opened / resolved:** questions this chunk raised in your mind, and any
  earlier questions it answered.
- **Promises:** anything the document sets up or promises to deliver ("we'll show X",
  "the surprising part is coming"). Later, mark each promise paid off or not.
- **Bail check:** as this persona, would you stop reading here? If yes, record
  "STOP HERE — because ___" but KEEP READING so you can give complete feedback. Note if
  anything later would have won you back.

Keep these notes as you go (in your working context is fine). Be honest and specific —
cite chunk numbers.

## Final report
After the last chunk, write a report with these sections:

1. **One-line verdict** — would this persona have finished, and their overall take.
2. **Bail point** — the first chunk where this persona would realistically stop, and why
   (or "would have kept reading throughout"). For captive personas, where understanding or
   goodwill broke down instead.
3. **Engagement / boredom map** — the arc of interest across the document; call out the
   specific chunk ranges that dragged, and the ones that landed.
4. **Unmet setups (promises vs. payoffs)** — every promise/setup the document made and
   whether it was paid off. Highlight the ones that were dropped.
5. **Confusion points** — where you were confused and why, with chunk numbers.
6. **"What is this about?"** — how quickly (which chunk) you could confidently say what the
   document is about and why you should care. Too late? Never?
7. **Top fixes** — the few highest-leverage changes, in priority order.

Your final message IS the report — write it directly, no preamble.
```

If you're running multiple personas, launch their subagents in parallel (multiple Task
calls in one message).

### 5. Synthesize

Collect the per-persona reports. If one persona, present its report cleanly. If several,
lead with a short synthesis: where personas agree (likely real problems), where they
diverge (a fix good for one reader may hurt another — e.g. a director wants the bottom line
first, a free reader wants a hook first), and the highest-priority fixes overall. Then
include each persona's full report.

## Notes

- The reading subagent, not you, must do the cold read — you've already seen context that
  would spoil it. Your job is setup, orchestration, and synthesis.
- Images resolve relative to the source Markdown file. Remote image URLs are downloaded; if
  one can't be fetched it degrades to alt text automatically (a warning is printed). Pass
  `--base-url` when the Markdown came from a URL and still references images by relative path.
