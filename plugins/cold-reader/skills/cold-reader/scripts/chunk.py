#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "markdown-it-py>=3.0",
#     "pysbd>=0.3.4",
#     "requests>=2.31",
# ]
# ///
"""Split a Markdown document into numbered chunk files for the cold-reader skill.

Chunks approximate a natural reading unit rather than a single block. Sentences
(within a paragraph) and items (within a list) are greedily accumulated into the
current chunk until adding the next one would push it past --target-words, at
which point the chunk is flushed. Short paragraphs and list items therefore merge
together instead of becoming one tiny chunk each: a 5-item list of short bullets
becomes one chunk (or two), not five, and a run of one-line paragraphs merges up
toward the target. A genuinely long paragraph still splits at sentence boundaries
near the target. Headings, images, code/tables and other media are hard breaks:
the accumulator flushes before them and never merges across them.

A chunk that has reached --min-words is flushed at a soft boundary (end of a
paragraph or list); below that floor it keeps accumulating across the boundary so
a chunk is not emitted far below target just because a block ended.

Output: <workdir>/chunk-001.md, chunk-002.png, chunk-003.md, ...
Zero-padded so the files sort in reading order. Images are extracted to their
own chunk file so a vision agent can Read them; with --no-vision an image becomes
a chunk-NNN.md containing "Image: <alt text>" as a screen reader would announce it.

Exit 0 means the setup is valid; there is no separate validation step.
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import re
import sys
import urllib.parse
import warnings
from pathlib import Path

# pysbd ships regex strings that trigger SyntaxWarning on 3.12+; keep stderr clean
# for the image warnings we actually emit.
warnings.filterwarnings("ignore", category=SyntaxWarning)

import pysbd
import requests
from markdown_it import MarkdownIt

# Block tokens that are hard breaks: read whole, standing as their own chunk.
# Blockquotes are here too — a quote is a distinct reading unit and its ">" lines
# would be mangled if sentences were merged with surrounding prose.
ATOMIC = {"heading", "fence", "code_block", "table", "html_block", "math_block", "blockquote"}
# Block tokens whose sentences feed the accumulator.
PROSE = {"paragraph"}

IMG_MD = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"[^\"]*\")?\)\s*$")
IMG_HTML = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_HTML_SRC = re.compile(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
IMG_HTML_ALT = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)


def top_level_blocks(md_text: str):
    """Yield (type, source_text) for each top-level block in reading order."""
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(md_text)
    lines = md_text.split("\n")

    def slice_lines(mapping):
        start, end = mapping
        return "\n".join(lines[start:end]).strip("\n")

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.level != 0 or tok.map is None:
            i += 1
            continue

        if tok.type in ("bullet_list_open", "ordered_list_open"):
            # Emit each top-level list item as its own block.
            depth = 0
            j = i
            while j < len(tokens):
                t = tokens[j]
                if t.type == "list_item_open" and t.level == 1 and t.map:
                    yield ("list_item", slice_lines(t.map))
                if t.type in ("bullet_list_open", "ordered_list_open"):
                    depth += 1
                elif t.type in ("bullet_list_close", "ordered_list_close"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue

        if tok.type.endswith("_open"):
            block_type = tok.type[: -len("_open")]
            yield (block_type, slice_lines(tok.map))
            # Skip to the matching close at this level.
            close = tok.type[: -len("_open")] + "_close"
            depth = 0
            j = i
            while j < len(tokens):
                if tokens[j].type == tok.type:
                    depth += 1
                elif tokens[j].type == close:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue

        if tok.type in ("fence", "code_block", "html_block", "hr", "math_block"):
            if tok.type != "hr":  # horizontal rules are not worth reading
                yield (tok.type, slice_lines(tok.map))
            i += 1
            continue

        i += 1


def detect_image(block_type: str, text: str):
    """Return (alt, src) if this block is a standalone image, else None."""
    stripped = text.strip()
    m = IMG_MD.match(stripped)
    if m:
        return m.group("alt"), m.group("src")
    if block_type == "html_block" or IMG_HTML.search(stripped):
        tag = IMG_HTML.search(stripped)
        if tag and not re.sub(IMG_HTML, "", stripped).strip():
            src_m = IMG_HTML_SRC.search(tag.group(0))
            alt_m = IMG_HTML_ALT.search(tag.group(0))
            if src_m:
                return (alt_m.group(1) if alt_m else ""), src_m.group(1)
    return None


def sentences_of(text: str, segmenter) -> list[str]:
    """Split prose into whole sentences, falling back to the block as one unit."""
    sentences = [s.strip() for s in segmenter.segment(text) if s.strip()]
    return sentences or [text.strip()]


class Accumulator:
    """Greedily packs sentence/item units into readable chunks.

    A unit is added with the separator that joins it to the previous unit if the
    two land in the same chunk. Adding a unit that would push the running chunk
    past ``target_words`` flushes first (the ceiling). ``soft_boundary`` — called
    at the end of a paragraph or list — flushes only once the chunk has reached
    ``min_words`` (the floor), so small blocks keep accumulating across the break.
    Hard units (headings, images, code, tables) flush the accumulator and stand
    alone; nothing merges across them.
    """

    def __init__(self, target_words: int, min_words: int):
        self.target_words = target_words
        self.min_words = min_words
        self.parts: list[tuple[str, str]] = []  # (separator-before, text)
        self.words = 0
        self.chunks: list[tuple[str, object]] = []

    def _flush(self) -> None:
        if not self.parts:
            return
        text = self.parts[0][1]
        for sep, part in self.parts[1:]:
            text += sep + part
        self.chunks.append(("text", text))
        self.parts = []
        self.words = 0

    def add_soft(self, text: str, sep: str) -> None:
        n = len(text.split())
        if self.parts and self.words + n > self.target_words:
            self._flush()
        self.parts.append((sep, text))
        self.words += n

    def soft_boundary(self) -> None:
        if self.words >= self.min_words:
            self._flush()

    def add_hard(self, chunk: tuple[str, object]) -> None:
        self._flush()
        self.chunks.append(chunk)

    def finish(self) -> list[tuple[str, object]]:
        self._flush()
        return self.chunks


def resolve_image_bytes(src: str, base_dir: Path, base_url: str | None = None):
    """Return (bytes, suggested_extension) for a local path, URL, or data URI."""
    if src.startswith("data:"):
        header, _, data = src.partition(",")
        ext = mimetypes.guess_extension(header[5:].split(";")[0]) or ".png"
        raw = base64.b64decode(data) if ";base64" in header else urllib.parse.unquote_to_bytes(data)
        return raw, ext
    if base_url and not src.startswith(("http://", "https://")):
        # Page-relative src (e.g. from a fetched HTML page): resolve it against
        # the document's source URL so it can be downloaded rather than read locally.
        src = urllib.parse.urljoin(base_url, src)
    if src.startswith(("http://", "https://")):
        resp = requests.get(src, timeout=30)
        resp.raise_for_status()
        ext = os.path.splitext(urllib.parse.urlparse(src).path)[1]
        if not ext:
            ext = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0]) or ".png"
        return resp.content, ext
    path = (base_dir / urllib.parse.unquote(src)).resolve()
    return path.read_bytes(), path.suffix or ".png"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Path to the source Markdown file")
    ap.add_argument("--workdir", required=True, help="Output directory for chunk files")
    ap.add_argument("--title", help="Document title; emitted as the first chunk (an H1), the way a real reader sees it before the body")
    ap.add_argument("--base-url", help="Original page URL; page-relative image srcs (e.g. images/foo.png from a fetched HTML page) are resolved against it and downloaded")
    ap.add_argument("--target-words", type=int, default=90, help="Target words per chunk; the upper bound where a chunk is flushed (default: 90)")
    ap.add_argument("--min-words", type=int, default=None, help="Floor below which a chunk keeps accumulating past a paragraph/list boundary (default: half of --target-words)")
    ap.add_argument("--no-vision", action="store_true", help="Replace images with 'Image: <alt text>' instead of extracting them")
    args = ap.parse_args()

    min_words = args.min_words if args.min_words is not None else max(1, args.target_words // 2)
    min_words = min(min_words, args.target_words)

    source = Path(args.source)
    if not source.is_file():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 1
    base_dir = source.parent
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    segmenter = pysbd.Segmenter(language="en", clean=False)
    md_text = source.read_text(encoding="utf-8")

    acc = Accumulator(args.target_words, min_words)
    image_count = 0
    image_errors: list[str] = []

    if args.title and args.title.strip():
        # A real reader sees the title first; give it its own leading chunk as an H1.
        title = args.title.strip().lstrip("#").strip()
        acc.add_hard(("text", f"# {title}"))

    prev_type: str | None = None
    for block_type, text in top_level_blocks(md_text):
        if not text.strip():
            prev_type = None
            continue
        img = detect_image(block_type, text)
        if img is not None:
            alt, src = img
            if args.no_vision:
                acc.add_hard(("text", f"Image: {alt}" if alt.strip() else "Image: (no alt text provided)"))
            else:
                try:
                    raw, ext = resolve_image_bytes(src, base_dir, args.base_url)
                    acc.add_hard((f"image:{ext}", raw))
                    image_count += 1
                except Exception as e:  # noqa: BLE001 - degrade to alt text on any failure
                    image_errors.append(f"{src}: {e}")
                    acc.add_hard(("text", f"Image: {alt}" if alt.strip() else "Image: (image could not be loaded)"))
            prev_type = None
            continue
        if block_type in ATOMIC:
            # Headings, code, tables, etc. are hard breaks read whole.
            acc.add_hard(("text", text))
            prev_type = None
        elif block_type in PROSE:
            # Feed sentences; the first joins to any prior block across a blank line.
            for i, sent in enumerate(sentences_of(text, segmenter)):
                acc.add_soft(sent, " " if i > 0 else "\n\n")
            acc.soft_boundary()
            prev_type = block_type
        elif block_type == "list_item":
            # Consecutive items share a chunk (joined tight); a new list starts a block.
            acc.add_soft(text, "\n" if prev_type == "list_item" else "\n\n")
            acc.soft_boundary()
            prev_type = block_type
        else:
            acc.add_hard(("text", text))
            prev_type = None

    chunks = acc.finish()
    width = max(3, len(str(len(chunks))))
    for idx, (kind, payload) in enumerate(chunks, start=1):
        stem = f"chunk-{idx:0{width}d}"
        if kind == "text":
            (workdir / f"{stem}.md").write_text(payload + "\n", encoding="utf-8")
        else:
            ext = kind.split(":", 1)[1]
            (workdir / f"{stem}{ext}").write_bytes(payload)

    print(f"workdir: {workdir}")
    print(f"chunks: {len(chunks)}")
    print(f"images: {image_count}{' (no-vision: alt text only)' if args.no_vision else ''}")
    for err in image_errors:
        print(f"image warning: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
