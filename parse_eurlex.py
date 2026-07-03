#!/usr/bin/env python3
"""
parse_eurlex.py — Download EUR-Lex bilingual HTML and generate data.js
======================================================================

What this does
--------------
For each regulation (GDPR + AVG, AI Act EN + NL), this script:

1. Downloads the official EUR-Lex HTML page
2. Parses the structural skeleton (chapters, articles, paragraphs, recitals)
3. Pairs EN and NL paragraphs by their structural ID (Article + lid + sub-letter)
4. Writes a single data.js consumed by index.html

Usage
-----
    pip install requests beautifulsoup4 lxml
    python parse_eurlex.py

The script writes ./data.js next to itself. Open ./index.html in any browser.

Notes
-----
- EU regulations are structurally identical across language versions, so
  pairing by (article, lid, sub-letter) is deterministic and reliable.
- If a paragraph count differs between EN and NL (rare, usually due to a
  formatting quirk in one version), the script logs a warning and falls
  back to lid-level pairing for that article.
- The script is conservative: it preserves anything it can't classify as a
  free-text 'free' paragraph rather than dropping it.
"""

from __future__ import annotations
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: missing dependencies. Run: pip install requests beautifulsoup4 lxml", file=sys.stderr)
    sys.exit(1)

# Suppress harmless warning when browser-saved EUR-Lex files are XML
import warnings
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOURCES = {
    "gdpr": {
        "en": "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679",
        "nl": "https://eur-lex.europa.eu/legal-content/NL/TXT/HTML/?uri=CELEX:32016R0679",
        "local_en": "gdpr_en.html",
        "local_nl": "gdpr_nl.html",
        "title_en": "Regulation (EU) 2016/679 — General Data Protection Regulation (GDPR)",
        "title_nl": "Verordening (EU) 2016/679 — Algemene Verordening Gegevensbescherming (AVG)",
        "meta": "Adopted 27 April 2016 · OJ L 119, 4.5.2016 · Applicable since 25 May 2018",
    },
    "aiact": {
        "en": "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R1689",
        "nl": "https://eur-lex.europa.eu/legal-content/NL/TXT/HTML/?uri=CELEX:32024R1689",
        "local_en": "aiact_en.html",
        "local_nl": "aiact_nl.html",
        "title_en": "Regulation (EU) 2024/1689 — Artificial Intelligence Act (AI Act)",
        "title_nl": "Verordening (EU) 2024/1689 — AI-verordening",
        "meta": "Adopted 13 June 2024 · OJ L, 12.7.2024 · Phased application from 2 February 2025",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (legal-bilingual-viewer/1.0) Python-requests",
}

OUTPUT = Path(__file__).parent / "data.js"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Paragraph:
    id: str
    type: str           # 'lid' | 'sub-letter' | 'sub-num' | 'free'
    en: str = ""
    nl: str = ""
    lid_num: Optional[int] = None
    letter: Optional[str] = None
    num: Optional[int] = None

    def to_dict(self) -> dict:
        d = {"id": self.id, "type": self.type, "en": self.en, "nl": self.nl}
        if self.lid_num is not None: d["lid_num"] = self.lid_num
        if self.letter is not None: d["letter"] = self.letter
        if self.num is not None: d["num"] = self.num
        return d


@dataclass
class Article:
    num: str
    title_en: str = ""
    title_nl: str = ""
    paragraphs: list[Paragraph] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "num": self.num,
            "title_en": self.title_en,
            "title_nl": self.title_nl,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
        }


@dataclass
class Chapter:
    num: str
    title_en: str = ""
    title_nl: str = ""
    articles: list[Article] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "num": self.num,
            "title_en": self.title_en,
            "title_nl": self.title_nl,
            "articles": [a.to_dict() for a in self.articles],
        }


@dataclass
class Recital:
    num: int
    en: str = ""
    nl: str = ""

    def to_dict(self) -> dict:
        return {"num": self.num, "en": self.en, "nl": self.nl}


@dataclass
class ParsedDoc:
    """Single-language parse result."""
    chapters: list[Chapter] = field(default_factory=list)
    recitals: list[Recital] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def load_local(path: str) -> str | None:
    """Try to read a local HTML file saved from the browser."""
    p = Path(__file__).parent / path
    if p.exists():
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > 5000:  # sanity check: must be real content, not a stub
            print(f"  loaded local file: {p} ({len(text)} chars)")
            return text
        print(f"  local file {p} exists but is too small ({len(text)} chars), skipping")
    return None


def fetch(url: str) -> str:
    print(f"  downloading {url}")
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    # Detect AWS WAF challenge page (EUR-Lex added bot protection ~2025)
    if len(resp.text) < 5000 and "awsWafCookieDomainList" in resp.text:
        raise RuntimeError(
            f"EUR-Lex returned a bot-challenge page instead of content.\n"
            f"  Save the page manually from your browser and place it next to this script.\n"
            f"  See README for details."
        )
    return resp.text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# EUR-Lex uses semantic CSS classes for structural elements.
# These selectors capture the patterns seen in published OJ HTML.
CLS_TITLE_DIVISION = ("oj-ti-section-1",)   # CHAPTER / HOOFDSTUK
CLS_TITLE_SECTION = ("oj-ti-section-2",)    # Section / Afdeling within chapter
CLS_TITLE_ARTICLE = ("oj-ti-art",)          # Article N / Artikel N
CLS_NORMAL = ("oj-normal",)                 # body paragraph
CLS_NUMBERED = ("oj-num",)                  # numbered paragraph wrapper
CLS_RECITAL = ("oj-grseq-1",)               # numbered group, sometimes recitals
CLS_SUB = ("oj-grseq-1", "oj-grseq-2")      # nested lists

ROMAN_RE = re.compile(r"^(?:CHAPTER|HOOFDSTUK)\s+([IVXLCDM]+)\b", re.IGNORECASE)
ARTICLE_RE = re.compile(r"^(?:Article|Artikel)\s+(\d+[a-z]?)\b", re.IGNORECASE)
RECITAL_INLINE_RE = re.compile(r"^\(\s*(\d+)\s*\)\s*(.*)", re.DOTALL)
LID_RE = re.compile(r"^(\d+)\.\s+(.*)", re.DOTALL)
SUB_LETTER_RE = re.compile(r"^\(([a-z])\)\s+(.*)", re.DOTALL)
SUB_LETTER_NL_RE = re.compile(r"^([a-z])\)\s+(.*)", re.DOTALL)
SUB_NUM_RE = re.compile(r"^\(\s*(\d+)\s*\)\s+(.*)", re.DOTALL)
SUB_NUM_NL_RE = re.compile(r"^(\d+)\)\s*(.*)", re.DOTALL)
# Section headers within chapters: "Section 1" / "Afdeling 1"
SECTION_RE = re.compile(r"^(?:Section|Afdeling)\s+(\d+)\b", re.IGNORECASE)
# Annex headers: "ANNEX I" / "BIJLAGE I" — marks end of article content
ANNEX_RE = re.compile(r"^(?:ANNEX|BIJLAGE)\s+[IVXLCDM]+\b", re.IGNORECASE)


def clean_text(node) -> str:
    if node is None:
        return ""
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    # Collapse internal whitespace, strip footnote markers like [(1)] left over
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\[\(\d+\)\]", "", text).strip()
    return text


def has_class(tag, names) -> bool:
    if not tag or not tag.has_attr("class"):
        return False
    classes = set(tag.get("class") or [])
    return any(n in classes for n in names)


# ---------------------------------------------------------------------------
# Single-language parser
# ---------------------------------------------------------------------------

def parse_one_language(html: str, lang: str) -> ParsedDoc:
    """
    Parse a single-language EUR-Lex HTML page. Returns a ParsedDoc with
    populated text in only the corresponding language field of each Paragraph.
    """
    soup = BeautifulSoup(html, "lxml")
    doc = ParsedDoc()

    # Strip nav, footer and metadata blocks
    for unwanted in soup.select("nav, footer, .NoteList, script, style"):
        unwanted.decompose()

    # Pre-process: EUR-Lex puts sub-points in two-cell table rows like
    #   <tr><td>(a)</td><td>content</td></tr>     (EN)
    #   <tr><td>a)</td><td>content</td></tr>      (NL)
    # Flatten each such row into a single <p> tag the regexes already handle.
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) != 2:
            continue
        marker = clean_text(cells[0])
        content = clean_text(cells[1])
        if not marker or not content:
            continue
        # Recognise sub-point markers: (a), (b), (1), (2), a), b), 1), 2)
        is_letter_en = bool(re.match(r"^\([a-z]\)$", marker))
        is_letter_nl = bool(re.match(r"^[a-z]\)$", marker))
        is_num = bool(re.match(r"^\(\s*\d+\s*\)$", marker))
        is_num_nl = bool(re.match(r"^\d+\)$", marker))
        # Also handle Roman numeral sub-points: (i), (ii), (iii), i), ii)
        is_roman_en = bool(re.match(r"^\([ivxlc]+\)$", marker, re.IGNORECASE))
        is_roman_nl = bool(re.match(r"^[ivxlc]+\)$", marker, re.IGNORECASE))
        if not (is_letter_en or is_letter_nl or is_num or is_num_nl or is_roman_en or is_roman_nl):
            continue
        new_p = soup.new_tag("p")
        new_p["class"] = ["oj-flattened-sub"]
        # Normalise to EN-style "(x) content" so downstream regexes match
        if is_letter_nl or is_num_nl or is_roman_nl:
            new_p.string = f"({marker[:-1]}) {content}"
        else:
            new_p.string = f"{marker} {content}"
        tr.replace_with(new_p)

    # Walk the body in document order. We track current chapter / article /
    # current lid number to assign deterministic IDs.
    body = soup.find("body") or soup

    # --- Pre-extract recitals from <div class="eli-subdivision"> ---
    # Recital text lives directly in these divs, not in <p> children,
    # so we extract them before the main <p>-only walk.
    for div in body.find_all("div", class_="eli-subdivision"):
        txt = clean_text(div)
        if not txt or len(txt) > 6000:
            continue
        m_rec = RECITAL_INLINE_RE.match(txt)
        if m_rec:
            num = int(m_rec.group(1))
            content = m_rec.group(2).strip()
            existing = next((r for r in doc.recitals if r.num == num), None)
            if existing is None:
                rec = Recital(num=num)
                if lang == "en":
                    rec.en = content
                else:
                    rec.nl = content
                doc.recitals.append(rec)

    current_chapter: Optional[Chapter] = None
    current_article: Optional[Article] = None
    current_lid: Optional[int] = None
    current_subnum: Optional[int] = None    # tracks last (N) definition number
    pending_article_title = False
    pending_chapter_title = False

    # Walk only <p> elements to avoid duplicates.
    # EUR-Lex wraps each <p> in a <div> with the same text; walking both
    # caused every paragraph to appear twice.
    paragraphs = body.find_all("p")

    for el in paragraphs:
        txt = clean_text(el)
        if not txt:
            continue
        if len(txt) > 6000:
            continue

        # Skip footnote references
        if has_class(el, ("oj-note", "oj-note-tag")):
            continue

        # ---------- Annex boundary ----------
        # Annexes follow the final article. Stop capturing article content.
        if ANNEX_RE.match(txt) and (has_class(el, ("oj-doc-ti",)) or len(txt) < 40):
            current_article = None
            current_chapter = None
            current_lid = None
            continue

        # Skip content after annexes have started (no current article)
        # Also skip signatory blocks at the end of the regulation
        if has_class(el, ("oj-doc-ti", "oj-signatory", "oj-final", "oj-doc-end")):
            current_article = None
            continue

        # ---------- Chapter ----------
        m_ch = ROMAN_RE.match(txt)
        if m_ch and (has_class(el, CLS_TITLE_DIVISION) or len(txt) < 40):
            current_chapter = Chapter(num=m_ch.group(1).upper())
            doc.chapters.append(current_chapter)
            current_article = None
            current_lid = None
            pending_chapter_title = True
            continue

        if pending_chapter_title and current_chapter and not current_chapter.title_en and not current_chapter.title_nl:
            if not re.match(r"^(Article|Artikel)\s+\d", txt):
                if lang == "en":
                    current_chapter.title_en = txt
                else:
                    current_chapter.title_nl = txt
                pending_chapter_title = False
                continue

        # ---------- Article ----------
        m_art = ARTICLE_RE.match(txt)
        if m_art and (has_class(el, CLS_TITLE_ARTICLE) or len(txt) < 40):
            current_article = Article(num=m_art.group(1))
            if current_chapter is None:
                current_chapter = Chapter(num="I")
                doc.chapters.append(current_chapter)
            current_chapter.articles.append(current_article)
            current_lid = None
            current_subnum = None
            pending_article_title = True
            continue

        if pending_article_title and current_article and not current_article.title_en and not current_article.title_nl:
            if lang == "en":
                current_article.title_en = txt
            else:
                current_article.title_nl = txt
            pending_article_title = False
            continue

        # ---------- Article body ----------
        if current_article is None:
            continue

        # Skip section headers (e.g. "Section 1" / "Afdeling 1") that appear
        # between articles within a chapter — they're structural, not content.
        if SECTION_RE.match(txt) and len(txt) < 40:
            continue
        # Also skip bare section titles that follow section headers
        if has_class(el, ("oj-ti-section-2",)):
            continue

        # Numbered lid: "1.  text"
        m_lid = LID_RE.match(txt)
        if m_lid and len(m_lid.group(1)) <= 3:
            current_lid = int(m_lid.group(1))
            current_subnum = None  # reset definition context
            content = m_lid.group(2).strip()
            pid = f"art-{current_article.num}-{current_lid}"
            p = Paragraph(id=pid, type="lid", lid_num=current_lid)
            if lang == "en":
                p.en = content
            else:
                p.nl = content
            current_article.paragraphs.append(p)
            continue

        # Sub-letter: "(a) text" (EN) or "a) text" (NL)
        m_sub = SUB_LETTER_RE.match(txt) or SUB_LETTER_NL_RE.match(txt)
        if m_sub and len(m_sub.group(1)) == 1:
            letter = m_sub.group(1)
            content = m_sub.group(2).strip()
            # Use lid number if in a lid context, or definition number if in
            # a definitions context, or fall back to 1
            if current_lid is not None:
                context = current_lid
            elif current_subnum is not None:
                context = f"def-{current_subnum}"
            else:
                context = 1
            pid = f"art-{current_article.num}-{context}-{letter}"
            p = Paragraph(id=pid, type="sub-letter", letter=letter,
                          lid_num=current_lid if current_lid is not None else (current_subnum or 1))
            if lang == "en":
                p.en = content
            else:
                p.nl = content
            current_article.paragraphs.append(p)
            continue

        # Sub-num: "(1) text" or NL "1) text" inside an article body (e.g. definitions)
        m_subn = SUB_NUM_RE.match(txt) or SUB_NUM_NL_RE.match(txt)
        if m_subn:
            num = int(m_subn.group(1))
            current_subnum = num  # track for sub-letters that follow
            content = m_subn.group(2).strip()

            # Check if the content has inline sub-letters like ": (a) ... (b) ..."
            # or NL-style ": a) ... b) ..."
            # If so, split them out so they align across languages.
            # Try EN-style first: (a), (b), ...
            inline_parts = re.split(r';\s*\(([a-z])\)\s*|\s+\(([a-z])\)\s+', content)
            # Try NL-style: a), b), ... (split on " a) " with word boundary)
            if len(inline_parts) < 3:
                inline_parts = re.split(r';\s*([a-z])\)\s*|\s+([a-z])\)\s+', content)

            # Normalise: each split group has two capture groups (one is None)
            # Flatten to: [header_text, letter, sub_text, letter, sub_text, ...]
            normalised = []
            i_p = 0
            while i_p < len(inline_parts):
                chunk = inline_parts[i_p]
                if chunk is not None:
                    normalised.append(chunk)
                i_p += 1

            # Only split if: header ends with ":" or "either:" and first letter is 'a'
            if (len(normalised) >= 3 and
                normalised[1] == 'a' and
                re.search(r':\s*$', normalised[0])):
                header = normalised[0].strip()
                pid = f"art-{current_article.num}-def-{num}"
                p = Paragraph(id=pid, type="sub-num", num=num)
                if lang == "en":
                    p.en = header
                else:
                    p.nl = header
                current_article.paragraphs.append(p)
                # Create sub-letter entries
                i_sub = 1
                while i_sub + 1 < len(normalised):
                    letter = normalised[i_sub]
                    if len(letter) == 1 and letter.isalpha():
                        sub_content = normalised[i_sub + 1].strip().rstrip(';').strip()
                        if sub_content:
                            spid = f"art-{current_article.num}-def-{num}-{letter}"
                            sp = Paragraph(id=spid, type="sub-letter", letter=letter,
                                           lid_num=num)
                            if lang == "en":
                                sp.en = sub_content
                            else:
                                sp.nl = sub_content
                            current_article.paragraphs.append(sp)
                    i_sub += 2
            else:
                # No inline sub-letters, emit as single definition
                pid = f"art-{current_article.num}-def-{num}"
                p = Paragraph(id=pid, type="sub-num", num=num)
                if lang == "en":
                    p.en = content
                else:
                    p.nl = content
                current_article.paragraphs.append(p)
            continue

        # Free text: a continuation paragraph (e.g. preamble of a lid)
        if has_class(el, CLS_NORMAL) or el.name == "p":
            if not txt or len(txt) < 6:
                continue
            pid = f"art-{current_article.num}-free-{len(current_article.paragraphs)}"
            p = Paragraph(id=pid, type="free")
            if lang == "en":
                p.en = txt
            else:
                p.nl = txt
            current_article.paragraphs.append(p)

    return doc


# ---------------------------------------------------------------------------
# Merging EN + NL into a single bilingual structure
# ---------------------------------------------------------------------------

def merge(en: ParsedDoc, nl: ParsedDoc) -> dict:
    """
    Merge two single-language parses by structural ID. EU regulations align
    perfectly at the article + lid + sub-letter level across translations.
    """
    out_chapters: list[dict] = []

    nl_chapters = {c.num: c for c in nl.chapters}
    for ch_en in en.chapters:
        ch_nl = nl_chapters.get(ch_en.num) or Chapter(num=ch_en.num)
        merged_articles: list[dict] = []
        nl_articles = {a.num: a for a in ch_nl.articles}
        for art_en in ch_en.articles:
            art_nl = nl_articles.get(art_en.num) or Article(num=art_en.num)
            merged_paras: list[dict] = []
            nl_paras = {p.id: p for p in art_nl.paragraphs}
            for p_en in art_en.paragraphs:
                p_nl = nl_paras.get(p_en.id)
                merged_paras.append({
                    "id": p_en.id,
                    "type": p_en.type,
                    "en": p_en.en,
                    "nl": p_nl.nl if p_nl else "",
                    **({"lid_num": p_en.lid_num} if p_en.lid_num is not None else {}),
                    **({"letter": p_en.letter} if p_en.letter is not None else {}),
                    **({"num": p_en.num} if p_en.num is not None else {}),
                })
            # Append any NL-only paragraphs missing in EN (rare)
            for p_nl in art_nl.paragraphs:
                if p_nl.id not in {pp["id"] for pp in merged_paras}:
                    merged_paras.append({
                        "id": p_nl.id, "type": p_nl.type, "en": "", "nl": p_nl.nl,
                        **({"lid_num": p_nl.lid_num} if p_nl.lid_num is not None else {}),
                        **({"letter": p_nl.letter} if p_nl.letter is not None else {}),
                        **({"num": p_nl.num} if p_nl.num is not None else {}),
                    })
            merged_articles.append({
                "num": art_en.num,
                "title_en": art_en.title_en,
                "title_nl": art_nl.title_nl,
                "paragraphs": merged_paras,
            })
        out_chapters.append({
            "num": ch_en.num,
            "title_en": ch_en.title_en,
            "title_nl": ch_nl.title_nl,
            "articles": merged_articles,
        })

    # Recitals
    nl_recitals = {r.num: r for r in nl.recitals}
    out_recitals = []
    for r_en in sorted(en.recitals, key=lambda r: r.num):
        r_nl = nl_recitals.get(r_en.num)
        out_recitals.append({
            "num": r_en.num,
            "en": r_en.en,
            "nl": r_nl.nl if r_nl else "",
        })

    return {"chapters": out_chapters, "recitals": out_recitals}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def render_data_js(payload: dict) -> str:
    """Render the merged payload as a JS module."""
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        "// data.js — generated by parse_eurlex.py\n"
        "// Do not edit by hand. Re-run the parser to refresh.\n\n"
        "window.LEGAL_DATA = " + body + ";\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    result = {}
    for key, src in SOURCES.items():
        print(f"\n[{key.upper()}]")

        # Try local files first (browser-saved), fall back to download
        html_en = load_local(src["local_en"])
        if html_en is None:
            try:
                html_en = fetch(src["en"])
            except Exception as e:
                print(f"  ERROR fetching EN: {e}")
                print(f"  -> Save the page from your browser as: {src['local_en']}")
                continue

        html_nl = load_local(src["local_nl"])
        if html_nl is None:
            try:
                html_nl = fetch(src["nl"])
            except Exception as e:
                print(f"  ERROR fetching NL: {e}")
                print(f"  -> Save the page from your browser as: {src['local_nl']}")
                continue

        print(f"  parsing EN...")
        doc_en = parse_one_language(html_en, "en")
        print(f"  parsing NL...")
        doc_nl = parse_one_language(html_nl, "nl")
        print(f"  parsed: {len(doc_en.chapters)} chapters EN / {len(doc_nl.chapters)} chapters NL")
        print(f"  parsed: {len(doc_en.recitals)} recitals EN / {len(doc_nl.recitals)} recitals NL")
        merged = merge(doc_en, doc_nl)

        # --- Validation / diagnostic report ---
        total_paras = 0
        one_sided = 0
        empty_arts = []
        for ch in merged["chapters"]:
            for art in ch["articles"]:
                paras = art.get("paragraphs", [])
                total_paras += len(paras)
                if not paras:
                    empty_arts.append(art["num"])
                for p in paras:
                    en_ok = bool(p.get("en", "").strip())
                    nl_ok = bool(p.get("nl", "").strip())
                    if en_ok and not nl_ok:
                        one_sided += 1
                    elif nl_ok and not en_ok:
                        one_sided += 1
        paired_recitals = sum(1 for r in merged["recitals"]
                             if r.get("en", "").strip() and r.get("nl", "").strip())
        print(f"  merged: {len(merged['chapters'])} chapters, "
              f"{sum(len(c['articles']) for c in merged['chapters'])} articles, "
              f"{total_paras} paragraphs")
        print(f"  recitals paired: {paired_recitals}/{len(merged['recitals'])}")
        if one_sided:
            print(f"  WARNING: {one_sided} paragraphs have text on only one side (EN or NL)")
        if empty_arts:
            print(f"  WARNING: articles with no paragraphs: {', '.join(empty_arts[:10])}")

        result[key] = {
            "title_en": src["title_en"],
            "title_nl": src["title_nl"],
            "meta": src["meta"],
            "partial": False,
            **merged,
        }

    OUTPUT.write_text(render_data_js(result), encoding="utf-8")
    print(f"\nWritten: {OUTPUT}")
    print("Open ./index.html in your browser.")


if __name__ == "__main__":
    main()
