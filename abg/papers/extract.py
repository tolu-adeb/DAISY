"""PDF -> text by section, cut to fit a token budget.  Pure functions; the PDF itself is never executed or rendered."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

CHARS_PER_TOKEN = (
    3.6  # conservative for English prose with numbers; used only to stay under the caps
)

# numbered ("3 Data", "4.2 Results") or named section headings on their own line
_HEAD = re.compile(
    r"^\s*(?:(?:\d{1,2}(?:\.\d{1,2}){0,2}\.?|[IVX]{1,5}\.)\s+[A-Z][^\n]{2,80}|"
    r"(?:abstract|introduction|related work|literature review|background|data(?: and methods?)?|methodology|methods?|"
    r"model|empirical (?:results|analysis)|results|robustness(?: checks)?|discussion|conclusions?|"
    r"concluding remarks|references|bibliography|appendix(?: [a-z])?|acknowledge?ments)\.?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_DROP = re.compile(r"^(references|bibliography|acknowledge?ments)", re.IGNORECASE)
# the parts that say what was tested and how well, kept first when a paper has to be cut
_PRIORITY = (
    "abstract",
    "introduction",
    "data",
    "method",
    "model",
    "strategy",
    "empirical",
    "result",
    "robust",
    "transaction cost",
    "out-of-sample",
    "discussion",
    "conclusion",
)


class ExtractError(Exception):
    pass


@dataclass
class Paper:
    pages: int
    sections: list[tuple[str, str]] = field(default_factory=list)  # (heading, text)
    title_guess: str | None = None

    @property
    def chars(self) -> int:
        return sum(len(h) + len(t) for h, t in self.sections)


def pdf_text(pdf: bytes, max_pages: int = 120) -> tuple[list[str], dict]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - installed with the package
        raise ExtractError(
            "The bot is missing pypdf. On the bot's computer run: .venv\\Scripts\\pip install pypdf"
        ) from e
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001 - pypdf raises many types for bad passwords
                raise ExtractError(
                    "The PDF is password-protected. Upload a copy without a password."
                ) from None
        meta = {}
        try:
            md = reader.metadata or {}
            meta = {"title": str(md.get("/Title") or "").strip() or None}
        except Exception:  # noqa: BLE001 - metadata is optional; a broken info dict mustn't stop the text
            meta = {}
        pages = []
        for i, p in enumerate(reader.pages):
            if i >= max_pages:
                break
            try:
                pages.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001 - one unreadable page shouldn't lose the rest
                pages.append("")
        return pages, meta
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - any parser failure becomes a message for the website
        raise ExtractError(
            f"Couldn't read the PDF ({type(e).__name__}). Is it a complete, undamaged file?"
        ) from None


def _clean(t: str) -> str:
    t = t.replace("\x00", "").replace("\r", "\n")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # re-join words hyphenated across lines
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def split_sections(text: str) -> list[tuple[str, str]]:
    marks = [(m.start(), m.group(0).strip()) for m in _HEAD.finditer(text)]
    if not marks:
        return [("Text", text.strip())]
    out = []
    if marks[0][0] > 0 and text[: marks[0][0]].strip():
        out.append(("Front matter", text[: marks[0][0]].strip()))
    for i, (pos, head) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = (
            text[pos + len(head) : end].strip()
            if text[pos : pos + len(head)].strip() == head
            else text[pos:end].strip()
        )
        out.append((head, body))
    return out


def extract(pdf: bytes) -> Paper:
    pages, meta = pdf_text(pdf)
    text = _clean("\n\n".join(pages))
    if len(re.sub(r"\s", "", text)) < 400:
        raise ExtractError(
            "The PDF has almost no text (it may be scanned images). Upload a text PDF, or paste a link."
        )
    secs = []
    dropping = False
    for head, body in split_sections(text):
        name = re.sub(r"^[\dIVX.\s]+", "", head).strip()
        if _DROP.match(name):
            dropping = True  # references and what follows them (usually appendices)
            continue
        if dropping and not name.lower().startswith("appendix"):
            continue
        dropping = False if name.lower().startswith("appendix") else dropping
        secs.append((head, body))
    first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 12), None)
    return Paper(
        pages=len(pages), sections=secs or [("Text", text)], title_guess=meta.get("title") or first
    )


def fit(paper: Paper, max_tokens: int) -> tuple[str, bool]:
    """Sections as one text within ``max_tokens``; the key sections are kept first.  Returns (text, was_cut)."""
    budget = int(max_tokens * CHARS_PER_TOKEN)
    parts = [f"## {h}\n{t}" for h, t in paper.sections]
    whole = "\n\n".join(parts)
    if len(whole) <= budget:
        return whole, False

    def rank(i: int) -> tuple[int, int]:
        h = paper.sections[i][0].lower()
        p = next((k for k, w in enumerate(_PRIORITY) if w in h), len(_PRIORITY))
        return (p, i)

    marker = "\n[... cut to fit ...]"
    keep, used = set(), 0
    for i in sorted(range(len(parts)), key=rank):
        piece = parts[i]
        sep = 2 if keep else 0  # the blank line that joins it to the others
        if used + sep + len(piece) > budget:
            room = budget - used - sep - len(marker)
            if room > 2000:  # a partial section is better than none
                parts[i] = piece[:room] + marker
                keep.add(i)
            break
        keep.add(i)
        used += sep + len(piece)
    return "\n\n".join(parts[i] for i in sorted(keep)), True


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1
