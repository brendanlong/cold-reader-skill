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

One chunk per block (paragraph, heading, list item, table, code block, image).
A paragraph or list item stays whole unless it exceeds --target-words, in which
case it is split at sentence boundaries into groups up to that length. Authorial
paragraph breaks are always preserved (we split, never merge across them).

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

# Block tokens that are read whole and never split by sentence.
ATOMIC = {"heading", "fence", "code_block", "table", "html_block", "math_block"}
# Block tokens whose prose is split when longer than the target.
PROSE = {"paragraph", "blockquote"}

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


def split_prose(text: str, target_words: int, segmenter) -> list[str]:
    """Greedily pack whole sentences into chunks of up to ~target_words."""
    words = len(text.split())
    if words <= target_words:
        return [text]
    sentences = [s.strip() for s in segmenter.segment(text) if s.strip()]
    if len(sentences) <= 1:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for sent in sentences:
        n = len(sent.split())
        if current and count + n > target_words:
            chunks.append(" ".join(current))
            current, count = [], 0
        current.append(sent)
        count += n
    if current:
        chunks.append(" ".join(current))
    # Merge a tiny trailing chunk back into the previous one.
    if len(chunks) >= 2 and len(chunks[-1].split()) < target_words * 0.4:
        chunks[-2] = chunks[-2] + " " + chunks.pop()
    return chunks


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
    ap.add_argument("--target-words", type=int, default=90, help="Approx. words before a paragraph is split (default: 90)")
    ap.add_argument("--no-vision", action="store_true", help="Replace images with 'Image: <alt text>' instead of extracting them")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 1
    base_dir = source.parent
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    segmenter = pysbd.Segmenter(language="en", clean=False)
    md_text = source.read_text(encoding="utf-8")

    chunks: list[tuple[str, str]] = []  # (kind, payload) where kind is "text" or "image:<ext>"
    image_count = 0
    image_errors: list[str] = []

    if args.title and args.title.strip():
        # A real reader sees the title first; give it its own leading chunk as an H1.
        title = args.title.strip().lstrip("#").strip()
        chunks.append(("text", f"# {title}"))

    for block_type, text in top_level_blocks(md_text):
        if not text.strip():
            continue
        img = detect_image(block_type, text)
        if img is not None:
            alt, src = img
            if args.no_vision:
                chunks.append(("text", f"Image: {alt}" if alt.strip() else "Image: (no alt text provided)"))
            else:
                try:
                    raw, ext = resolve_image_bytes(src, base_dir, args.base_url)
                    chunks.append((f"image:{ext}", None))  # payload filled after we know the index
                    chunks[-1] = (f"image:{ext}", raw)
                    image_count += 1
                except Exception as e:  # noqa: BLE001 - degrade to alt text on any failure
                    image_errors.append(f"{src}: {e}")
                    chunks.append(("text", f"Image: {alt}" if alt.strip() else "Image: (image could not be loaded)"))
            continue
        if block_type in ATOMIC:
            chunks.append(("text", text))
        elif block_type in PROSE or block_type == "list_item":
            for piece in split_prose(text, args.target_words, segmenter):
                chunks.append(("text", piece))
        else:
            chunks.append(("text", text))

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
