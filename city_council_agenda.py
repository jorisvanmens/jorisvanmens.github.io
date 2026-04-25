#!/usr/bin/env python3
"""
Sausalito City Council Agenda Summarizer

Finds the next upcoming City Council meeting on the Granicus platform,
fetches its agenda, and uses Claude to summarize it — highlighting any
items related to cycling, pedestrian safety, and housing.

Writes output to:
  - stdout (plain text)
  - city-council/index.html (served at apps.jorisvanmens.com/city-council/)

Usage:
  python city_council_agenda.py                  # auto-discover next meeting
  python city_council_agenda.py --url <URL>      # use a specific agenda URL

Requires:
  ANTHROPIC_API_KEY environment variable to be set.
  pip install -r requirements.txt
"""

import argparse
import io
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import markdown as md_lib
import pdfplumber
import requests
import urllib3
from bs4 import BeautifulSoup

GRANICUS_BASE = "https://sausalito.granicus.com"
PUBLISHER_URL = f"{GRANICUS_BASE}/ViewPublisher.php?view_id=6"
AGENDA_URL_TEMPLATE = f"{GRANICUS_BASE}/AgendaViewer.php?view_id=6&event_id={{event_id}}"

HTML_OUTPUT_PATH = Path(__file__).parent / "city-council" / "index.html"
FINAL_HTML_OUTPUT_PATH = Path(__file__).parent / "city-council" / "final.html"
LAST_EVENT_ID_PATH = Path(__file__).parent / "city-council" / "last_event_id"
AGENDAS_DIR = Path(__file__).parent / "city-council" / "agendas"

# ── Email settings ────────────────────────────────────────────────────────────
# SENDER_EMAIL must be verified in SendGrid (Settings → Sender Authentication).
SENDER_EMAIL = ("City Council App", "city-council-app@jorisvanmens.com")

# Recipients are read from the EMAIL_RECIPIENTS environment variable (a GitHub
# Actions secret) so they are never stored in this public repository.
# Set the secret to a comma-separated list, e.g.: "a@example.com,b@example.com"
def get_recipients() -> list[str]:
    raw = os.environ.get("EMAIL_RECIPIENTS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_last_event_id() -> str:
    """Return the event_id from the previous successful run, or '' if none."""
    if LAST_EVENT_ID_PATH.exists():
        return LAST_EVENT_ID_PATH.read_text(encoding="utf-8").strip()
    return ""


def save_event_id(event_id: str) -> None:
    """Persist the event_id after a successful run."""
    LAST_EVENT_ID_PATH.parent.mkdir(exist_ok=True)
    LAST_EVENT_ID_PATH.write_text(event_id, encoding="utf-8")


class NoAgendaPublishedError(Exception):
    """Raised when the Granicus page loads correctly but no agenda is published yet."""


def find_next_agenda_url() -> str:
    """
    Scrape the Granicus publisher listing page to find the most
    recent/upcoming City Council meeting with a posted agenda.
    Returns the full agenda viewer URL.

    Raises NoAgendaPublishedError if the page loads and shows meeting listings
    but no agenda link is available yet.
    Raises an exception (HTTP error or ValueError) if the page fails to load
    or doesn't look like a valid Granicus listing.
    """
    print(f"Checking {PUBLISHER_URL} for upcoming meetings...")
    resp = requests.get(PUBLISHER_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    match = re.search(r"event_id=(\d+)", resp.text)
    if match:
        event_id = match.group(1)
        return AGENDA_URL_TEMPLATE.format(event_id=event_id)

    # No agenda link found — distinguish "not posted yet" from "broken page"
    page_looks_valid = any(
        marker in resp.text
        for marker in ("ViewPublisher", "AgendaViewer", "Granicus", "City Council")
    )
    if page_looks_valid:
        raise NoAgendaPublishedError(
            "Granicus page loaded and shows upcoming meetings, "
            "but no agenda has been published yet."
        )
    raise ValueError(
        "Granicus page loaded but doesn't look like a valid meeting listing "
        "(no recognisable content found). The page structure may have changed."
    )


def _get(url: str) -> requests.Response:
    """GET a URL, retrying without SSL verification on certificate errors.
    Granicus redirects agendas to S3 URLs whose bucket names contain underscores,
    which causes hostname validation to fail on some runners."""
    try:
        return requests.get(url, headers=HEADERS, timeout=30)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=HEADERS, timeout=30, verify=False)


def _pdf_links(pdf) -> list[str]:
    """Extract all unique hyperlink URLs from PDF annotations."""
    seen, links = set(), []
    for page in pdf.pages:
        for annot in page.annots or []:
            uri = annot.get("uri")
            if not uri:
                data = annot.get("data") or {}
                raw = data.get("URI", b"") if isinstance(data, dict) else b""
                uri = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
            if uri and isinstance(uri, str) and uri.startswith("http") and uri not in seen:
                seen.add(uri)
                links.append(uri)
    return links


def _parse_pdf(data: bytes) -> tuple[str, str]:
    """Extract (meeting_title, text) from raw PDF bytes using pdfplumber.
    Appends a list of hyperlinks found in the PDF so Claude can reference them."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
        links = _pdf_links(pdf)
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages))
    if links:
        text += "\n\nLINKS EMBEDDED IN AGENDA PDF:\n" + "\n".join(f"- {u}" for u in links)
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "City Council Meeting")
    return title, text


def _parse_html(resp: requests.Response) -> tuple[str, str]:
    """Extract (meeting_title, text) from an HTML response."""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    meeting_title = title_tag.get_text(strip=True) if title_tag else "City Council Meeting"
    main = (
        soup.find(id=re.compile(r"agenda|content|main", re.I))
        or soup.find("div", class_=re.compile(r"agenda|content|main", re.I))
        or soup.find("body")
    )
    raw_text = main.get_text(separator="\n", strip=True)
    return meeting_title, re.sub(r"\n{3,}", "\n\n", raw_text)


def fetch_agenda_text(agenda_url: str) -> tuple[str, str, str, bytes | None]:
    """
    Fetch a Granicus AgendaViewer URL.
    Returns (meeting_title, agenda_text, source_url, pdf_bytes) where
    source_url is the final URL after any redirects and pdf_bytes is the
    raw PDF content (or None if the agenda was served as HTML).
    """
    print(f"Fetching agenda from:\n  {agenda_url}")
    resp = _get(agenda_url)
    resp.raise_for_status()

    source_url = resp.url
    content_type = resp.headers.get("Content-Type", "")
    is_pdf = "pdf" in content_type or resp.url.lower().endswith(".pdf")

    if is_pdf:
        print(f"  → PDF detected ({resp.url}), extracting text with pdfplumber")
        title, text = _parse_pdf(resp.content)
        return title, text, source_url, resp.content
    else:
        title, text = _parse_html(resp)
        return title, text, source_url, None


def save_agenda_pdf(event_id: str, pdf_bytes: bytes) -> Path:
    """Save the raw agenda PDF to city-council/agendas/event_{event_id}_initial.pdf."""
    AGENDAS_DIR.mkdir(exist_ok=True)
    path = AGENDAS_DIR / f"event_{event_id}_initial.pdf"
    path.write_bytes(pdf_bytes)
    return path


def load_stored_pdf() -> tuple[str, str, str, str, bytes]:
    """
    Load the most recently saved agenda PDF from city-council/agendas/.
    Returns (meeting_title, agenda_text, source_url, event_id, pdf_bytes).
    source_url is reconstructed from the event_id (the original S3 URL is not stored).
    """
    if not AGENDAS_DIR.exists():
        raise FileNotFoundError(f"Agendas directory not found: {AGENDAS_DIR}")
    pdfs = sorted(AGENDAS_DIR.glob("event_*_initial.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        raise FileNotFoundError(f"No saved PDFs found in {AGENDAS_DIR}")

    pdf_path = pdfs[0]
    print(f"Loading stored PDF: {pdf_path.name}")
    pdf_bytes = pdf_path.read_bytes()
    title, text = _parse_pdf(pdf_bytes)

    match = re.match(r"event_(\d+)_initial\.pdf", pdf_path.name)
    event_id = match.group(1) if match else ""
    source_url = AGENDA_URL_TEMPLATE.format(event_id=event_id) if event_id else str(pdf_path)

    return title, text, source_url, event_id, pdf_bytes


def _pdf_links_from_bytes(pdf_bytes: bytes) -> list[str]:
    """Extract all hyperlink URLs from a PDF given as raw bytes."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return _pdf_links(pdf)


def extract_meeting_date(text: str) -> str:
    """Return the first recognisable long-form date found in the agenda text."""
    m = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        text[:3000],
        re.I,
    )
    return m.group(0) if m else ""


def extract_meeting_datetime(text: str) -> datetime | None:
    """
    Return a Pacific-timezone-aware datetime for the Regular Meeting / Open Session start time.
    Falls back to the last time found in the first 3000 characters if no labelled session found.
    """
    date_match = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        text[:3000], re.I,
    )
    if not date_match:
        return None

    # Prefer time explicitly labelled as Regular Meeting or Open Session
    labelled = re.search(
        r"(?:Regular\s+Meeting|Open\s+Session)\s*[:\-]?\s*(\d{1,2}:\d{2})\s*([AP]\.?M\.?)",
        text[:3000], re.I,
    )
    if labelled:
        time_str, ampm_raw = labelled.group(1), labelled.group(2)
    else:
        times = re.findall(r"\b(\d{1,2}:\d{2})\s*([AP]\.?M\.?)\b", text[:3000], re.I)
        if not times:
            return None
        time_str, ampm_raw = times[-1]

    ampm = re.sub(r"[^APMapm]", "", ampm_raw).upper()
    try:
        pacific = ZoneInfo("America/Los_Angeles")
        dt = datetime.strptime(f"{date_match.group(0)} {time_str} {ampm}", "%B %d, %Y %I:%M %p")
        return dt.replace(tzinfo=pacific)
    except ValueError:
        return None


def is_within_prefetch_window(meeting_dt: datetime, window_hours: float = 5.0) -> bool:
    """Return True if the current time is within window_hours before the meeting."""
    now = datetime.now(timezone.utc)
    hours_until = (meeting_dt.astimezone(timezone.utc) - now).total_seconds() / 3600
    return 0 < hours_until <= window_hours


def fetch_linked_document(url: str) -> str:
    """Fetch a URL and return its text content, handling both PDF and HTML."""
    resp = _get(url)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    is_pdf = "pdf" in content_type or url.lower().endswith(".pdf")
    if is_pdf:
        _, text = _parse_pdf(resp.content)
    else:
        _, text = _parse_html(resp)
    return text


def summarize_agenda(meeting_title: str, agenda_text: str, agenda_url: str) -> str:
    """
    Send the agenda text to Claude and return a structured Markdown summary.
    Section order: Overview → Topics of Interest → Full Agenda.
    Uses a cached system prompt to reduce token costs on repeated runs.
    """
    client = anthropic.Anthropic()

    system_prompt = (
        "You are an expert analyst of local government agendas with deep knowledge "
        "of urban planning, transportation policy, and housing policy. "
        "You help Sausalito residents quickly understand what their City Council is "
        "working on, with particular attention to topics affecting everyday quality "
        "of life: cycling infrastructure, pedestrian safety, and housing availability."
    )

    user_prompt = f"""Please analyze the following Sausalito City Council meeting agenda.

Produce a summary with exactly three sections:

## 1. Meeting Overview
State the meeting date and time(s) concisely on one line, e.g.:
"*Tuesday April 21, 2026 · Special Meeting (Closed Session) 3:30 PM · Regular Meeting 5:00 PM*"
Wrap it in Markdown italics as shown. Do NOT include the meeting location or Zoom/call-in details.
Then write 2–3 sentences summarizing the overall themes or most significant items.

## 2. Topics of Interest
Identify every agenda item related to any of the following, even if only tangentially. For each category that has matching items, use the exact Markdown sub-header shown below:

### 🚲 Cycling
Covers: bike lanes, bicycle infrastructure, bike-share, Caltrans roadway projects, multi-use paths, etc.

### 🚶 Pedestrian Safety
Covers: sidewalks, crosswalks, traffic calming, speed limits, Vision Zero, school safety zones, ADA accessibility, etc.

### 🏠 Housing
Covers: affordable housing, zoning or general plan amendments, development/subdivision approvals, ADUs, density bonuses, inclusionary requirements, housing element updates, etc.

For each relevant item include:
- The agenda item number and a brief description
- What action is being requested (vote, first reading, discussion only, public hearing, etc.)
- A **Links** sub-list. Include staff reports and any other documents listed under "LINKS EMBEDDED IN AGENDA PDF" that are relevant to this item, plus well-known external resources you are confident are accurate (sausalito.gov, Marin County, Caltrans, CA HCD, etc.). Format every link as descriptive Markdown text — [Staff Report](url), [Project Website](url) — never show a raw URL. Do NOT fabricate URLs.

If none of the three topics appear on the agenda, state that clearly.

## 3. Full Agenda
A concise bullet-point list of all substantive agenda items. Skip purely procedural items (call to order, roll call, approval of prior minutes, adjournment).

---
Agenda source: {agenda_url}
Meeting: {meeting_title}

{agenda_text}
"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


def summarize_agenda_changes(
    initial_text: str, final_text: str, meeting_title: str, agenda_url: str
) -> str:
    """Compare initial and final agenda texts with Claude, returning a Markdown diff summary."""
    client = anthropic.Anthropic()
    prompt = f"""Compare these two versions of a Sausalito City Council meeting agenda.

## Initial Agenda (posted earlier)
{initial_text[:8000]}

## Final Agenda (as of meeting day)
{final_text[:8000]}

List ONLY items that actually changed. For each changed item, write one or two sentences describing what changed. Do not mention items that remained the same.

Changes to note:
- Items added to or removed from the agenda
- Meaningful changes to an item's description or scope
- New staff reports or attachments added
- Items moved, continued, or withdrawn

If nothing changed, write a single sentence saying so.

Meeting: {meeting_title}
Source: {agenda_url}
"""
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def summarize_public_comments(
    comment_docs: list[tuple[str, str]], meeting_title: str
) -> str:
    """
    Summarize public comment documents with Claude.
    comment_docs is a list of (url, text) pairs.
    Returns a Markdown summary organized by topic with support/opposition counts.
    """
    if not comment_docs:
        return "No public comment documents were found linked in the final agenda."

    combined = "\n\n---\n\n".join(
        f"Source: {url}\n\n{text[:4000]}"
        for url, text in comment_docs
    )
    client = anthropic.Anthropic()
    prompt = f"""Analyze the following documents linked in a Sausalito City Council meeting agenda.

Do NOT add a title or meeting header at the top of your response. Start directly with the first agenda item.

For each agenda item that has public comments, use this structure:

### [Agenda Item Name]

Count the total comments for this item, then apply exactly one of these two formats:

**5 or fewer comments:** Write a one-sentence summary of each individual comment. Use the commenter's actual name if it appears in the document; never write "a resident" or "a community member".

**6 or more comments:** Write the support/opposition counts and a 2–3 sentence summary of the overall sentiment. Do not list individual comments.

Rules that apply in all cases:
- Do NOT include a "Key themes" section
- Do NOT produce long lists of names ("Notable supporters: …")
- Keep it concise

If a linked document is not a public comment (e.g. a staff report or technical study), give its title and a one-sentence summary.

Meeting: {meeting_title}

--- LINKED DOCUMENTS ---
{combined}
"""
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def write_html(
    meeting_title: str,
    summary_markdown: str,
    agenda_url: str,
    source_url: str,
    meeting_date: str,
) -> Path:
    """
    Convert the Markdown summary to an HTML page and write it to
    city-council/index.html. Returns the path written.
    """
    content_html = md_lib.markdown(summary_markdown, extensions=["extra"])

    now = datetime.now(timezone.utc)
    updated_str = now.strftime("%-d %B %Y at %-I:%M %p UTC")

    page_title = (
        f"Sausalito City Council: Upcoming Meeting — {meeting_date}"
        if meeting_date
        else "Sausalito City Council: Upcoming Meeting"
    )

    is_pdf = "pdf" in source_url.lower() or source_url.lower().endswith(".pdf")
    pdf_label = "Download full agenda PDF" if is_pdf else "View full agenda"
    pdf_icon = "📄"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: #f0f4f8;
      color: #1a2332;
      line-height: 1.65;
    }}

    /* ── Header ── */
    .site-header {{
      background: linear-gradient(135deg, #0c3547 0%, #1a6b8a 55%, #2eb8b8 100%);
      color: white;
      padding: 2.5rem 1.5rem 2rem;
    }}
    .header-inner {{
      max-width: 780px;
      margin: 0 auto;
    }}
    .site-header h1 {{
      font-size: 1.75rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      text-shadow: 0 1px 4px rgba(0,0,0,0.25);
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}
    .site-header .meeting-label {{
      font-size: 1rem;
      opacity: 0.85;
      margin-top: 0.3rem;
      font-weight: 400;
    }}
    /* ── PDF link bar ── */
    .pdf-bar {{
      background: #e8f4f8;
      border-bottom: 1px solid #bee3f8;
      padding: 0.6rem 1.5rem;
    }}
    .pdf-bar-inner {{
      max-width: 780px;
      margin: 0 auto;
      font-size: 0.9rem;
    }}
    .pdf-bar a {{
      color: #1a6b8a;
      text-decoration: none;
      font-weight: 600;
    }}
    .pdf-bar a:hover {{ text-decoration: underline; }}

    /* ── Content ── */
    .page {{
      max-width: 780px;
      margin: 0 auto;
      padding: 2rem 1.5rem 4rem;
    }}

    .summary h2 {{
      font-size: 1.1rem;
      font-weight: 700;
      color: #1e293b;
      margin: 2rem 0 0.6rem;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid #e2e8f0;
    }}
    .summary h2:first-child {{ margin-top: 0; }}
    .summary h3 {{
      font-size: 1rem;
      font-weight: 700;
      color: #1e3a5f;
      margin: 1.2rem 0 0.4rem;
    }}

    .summary p {{ margin: 0.6rem 0; color: #334155; }}

    .summary ul, .summary ol {{
      margin: 0.5rem 0 0.5rem 1.4rem;
      color: #334155;
    }}
    .summary li {{ margin: 0.3rem 0; }}

    .summary strong {{ color: #1e293b; font-weight: 600; }}

    .summary a {{ color: #1a6b8a; text-decoration: none; }}
    .summary a:hover {{ text-decoration: underline; }}

    /* ── Topics of Interest callout ── */
    .topics-callout {{
      background: #eff6ff;
      border-left: 4px solid #1d4ed8;
      border-radius: 0 8px 8px 0;
      padding: 1rem 1.25rem;
      margin-top: 0.6rem;
    }}
    .topics-callout p, .topics-callout li {{ color: #1e3a5f; }}
    .topics-callout a {{ color: #1d4ed8; }}

    /* ── Footer ── */
    .page-footer {{
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid #e2e8f0;
      font-size: 0.82rem;
      color: #94a3b8;
      line-height: 1.8;
    }}
    .page-footer a {{ color: #1a6b8a; text-decoration: none; }}
    .page-footer a:hover {{ text-decoration: underline; }}

    @media (max-width: 600px) {{
      .site-header h1 {{ font-size: 1.35rem; }}
      .page {{ padding: 1.25rem 1rem 3rem; }}
    }}
  </style>
</head>
<body>

  <header class="site-header">
    <div class="header-inner">
      <h1>⚓ Sausalito City Council: Upcoming Meeting</h1>
      <p class="meeting-label">{(meeting_date + " Meeting Agenda Summary") if meeting_date else "Meeting Agenda Summary"}</p>
    </div>
  </header>

  <div class="pdf-bar">
    <div class="pdf-bar-inner">
      {pdf_icon} <a href="{source_url}">{pdf_label}</a>
    </div>
  </div>

  <div class="page">
    <div class="summary" id="summary">
      {content_html}
    </div>

    <script>
      // Wrap Topics of Interest content in callout box
      document.querySelectorAll('#summary h2').forEach(h2 => {{
        if (h2.textContent.includes('Topics of Interest')) {{
          const wrapper = document.createElement('div');
          wrapper.className = 'topics-callout';
          const siblings = [];
          let node = h2.nextSibling;
          while (node) {{
            const next = node.nextSibling;
            if (node.nodeType === 1 && node.tagName === 'H2') break;
            siblings.push(node);
            node = next;
          }}
          h2.after(wrapper);
          siblings.forEach(n => wrapper.appendChild(n));
        }}
      }});

    </script>

    <footer class="page-footer">
      <p>Last updated: {updated_str}</p>
      <p>Contact: <a href="mailto:city-council-app@jorisvanmens.com">city-council-app@jorisvanmens.com</a></p>
    </footer>
  </div>

</body>
</html>
"""

    HTML_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    HTML_OUTPUT_PATH.write_text(html, encoding="utf-8")
    return HTML_OUTPUT_PATH


def _build_email_body(
    summary_markdown: str, source_url: str, meeting_date: str, updated_str: str
) -> str:
    """
    Build an email-safe HTML version of the summary.
    Uses inline styles and table layout (no JS, no CSS variables, no gradients).
    Pre-renders the Topics of Interest callout and topic icons that the web page
    handles via JavaScript.
    """
    content_html = md_lib.markdown(summary_markdown, extensions=["extra"])

    # Wrap Topics of Interest section in a callout div
    soup = BeautifulSoup(content_html, "html.parser")
    callout_style = (
        "background:#eff6ff; border-left:4px solid #1d4ed8; "
        "border-radius:0 6px 6px 0; padding:12px 16px; margin:8px 0;"
    )
    for h2 in soup.find_all("h2"):
        if "Topics of Interest" in h2.get_text():
            wrapper = soup.new_tag("div", style=callout_style)
            to_move = []
            node = h2.next_sibling
            while node:
                nxt = node.next_sibling
                if getattr(node, "name", None) == "h2":
                    break
                to_move.append(node)
                node = nxt
            h2.insert_after(wrapper)
            for n in to_move:
                wrapper.append(n.extract())

    # Apply inline styles to every element
    STYLES = {
        "h1": (
            "font-size:20px; font-weight:800; color:#0c3547; "
            "margin:24px 0 8px; padding-bottom:6px; border-bottom:2px solid #1a6b8a;"
        ),
        "h2": (
            "font-size:15px; font-weight:700; color:#1e293b; "
            "margin:20px 0 6px; padding-bottom:4px; border-bottom:1px solid #e2e8f0;"
        ),
        "h3": "font-size:14px; font-weight:700; color:#1e3a5f; margin:14px 0 4px;",
        "p":  "margin:6px 0; color:#334155; font-size:14px; line-height:1.6;",
        "ul": "margin:6px 0 6px 20px; color:#334155; font-size:14px;",
        "ol": "margin:6px 0 6px 20px; color:#334155; font-size:14px;",
        "li": "margin:3px 0; font-size:14px; color:#334155;",
        "a":  "color:#1a6b8a; text-decoration:none;",
        "strong": "color:#1e293b; font-weight:600;",
    }
    for tag, style in STYLES.items():
        for el in soup.find_all(tag):
            el["style"] = (el.get("style", "") + " " + style).strip()

    content_html = str(soup)

    is_pdf = "pdf" in source_url.lower()
    pdf_label = "Download full agenda PDF" if is_pdf else "View full agenda"
    meeting_label = f"{meeting_date} Meeting Agenda Summary" if meeting_date else "Meeting Agenda Summary"
    subtitle = meeting_label

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sausalito City Council: Upcoming Meeting — {meeting_label}</title>
</head>
<body style="margin:0; padding:0; background:#f0f4f8; font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f4f8;">
    <tr>
      <td align="center" style="padding:20px 10px;">
        <table width="600" cellpadding="0" cellspacing="0" border="0"
               style="max-width:600px; width:100%; background:#ffffff;
                      border-radius:8px; overflow:hidden;
                      box-shadow:0 2px 8px rgba(0,0,0,0.08);">
          <!-- Header -->
          <tr>
            <td style="background:#0c3547; padding:24px 28px;">
              <h1 style="margin:0; color:#ffffff; font-size:20px; font-weight:800;
                         letter-spacing:-0.3px; font-family:Arial,Helvetica,sans-serif;">
                ⚓ Sausalito City Council: Upcoming Meeting
              </h1>
              <p style="margin:6px 0 0; color:rgba(255,255,255,0.85); font-size:13px;
                        line-height:1.4; font-family:Arial,Helvetica,sans-serif;">
                {subtitle}
              </p>
            </td>
          </tr>
          <!-- PDF link bar -->
          <tr>
            <td style="background:#e8f4f8; padding:10px 28px;
                       border-bottom:1px solid #bee3f8; font-size:13px;
                       font-family:Arial,Helvetica,sans-serif;">
              📄 <a href="{source_url}"
                    style="color:#1a6b8a; font-weight:600; text-decoration:none;"
                  >{pdf_label}</a>
            </td>
          </tr>
          <!-- Summary content -->
          <tr>
            <td style="padding:20px 28px 16px; font-family:Arial,Helvetica,sans-serif;">
              {content_html}
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding:14px 28px; border-top:1px solid #e2e8f0;
                       font-size:11px; color:#94a3b8;
                       font-family:Arial,Helvetica,sans-serif;">
              Last updated: {updated_str} &nbsp;·&nbsp;
              <a href="mailto:{SENDER_EMAIL[1]}"
                 style="color:#1a6b8a; text-decoration:none;">{SENDER_EMAIL[1]}</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def write_final_html(
    meeting_title: str,
    changes_summary: str,
    comments_summary: str,
    agenda_url: str,
    source_url: str,
    meeting_date: str,
    public_comment_time: str = "",
) -> Path:
    """Write the final-agenda summary (changes + public comments) to city-council/final.html."""
    changes_html = md_lib.markdown(changes_summary, extensions=["extra"])
    comments_html = md_lib.markdown(comments_summary, extensions=["extra"])

    now = datetime.now(timezone.utc)
    updated_str = now.strftime("%-d %B %Y at %-I:%M %p UTC")

    page_title = (
        f"Sausalito City Council — {meeting_date} Meeting Day Summary"
        if meeting_date else "Sausalito City Council — Meeting Day Summary"
    )
    is_pdf = "pdf" in source_url.lower() or source_url.lower().endswith(".pdf")
    pdf_label = "Download final agenda PDF" if is_pdf else "View final agenda"
    subtitle_date = (meeting_date + " — ") if meeting_date else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: #f0f4f8;
      color: #1a2332;
      line-height: 1.65;
    }}

    /* ── Header ── */
    .site-header {{
      background: linear-gradient(135deg, #0c3547 0%, #1a6b8a 55%, #2eb8b8 100%);
      color: white;
      padding: 2.5rem 1.5rem 2rem;
    }}
    .header-inner {{ max-width: 780px; margin: 0 auto; }}
    .site-header h1 {{
      font-size: 1rem;
      font-weight: 600;
      opacity: 0.9;
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}
    .site-header .meeting-label {{
      font-size: 0.85rem;
      opacity: 0.7;
      margin-top: 0.2rem;
      font-weight: 400;
    }}

    /* ── PDF / nav bar ── */
    .pdf-bar {{
      background: #e8f4f8;
      border-bottom: 1px solid #bee3f8;
      padding: 0.6rem 1.5rem;
    }}
    .pdf-bar-inner {{
      max-width: 780px;
      margin: 0 auto;
      font-size: 0.9rem;
      display: flex;
      gap: 1.2rem;
      flex-wrap: wrap;
    }}
    .pdf-bar a {{ color: #1a6b8a; text-decoration: none; font-weight: 600; }}
    .pdf-bar a:hover {{ text-decoration: underline; }}

    /* ── Content ── */
    .page {{
      max-width: 780px;
      margin: 0 auto;
      padding: 2rem 1.5rem 4rem;
    }}

    .summary h2 {{
      font-size: 1.5rem;
      font-weight: 800;
      color: #0c3547;
      margin: 2.5rem 0 0.75rem;
      padding-bottom: 0.4rem;
      border-bottom: 2px solid #1a6b8a;
    }}
    .summary h2:first-child {{ margin-top: 0; }}
    .summary h3 {{
      font-size: 1rem;
      font-weight: 700;
      color: #1e293b;
      margin: 1.2rem 0 0.4rem;
    }}
    .summary p {{ margin: 0.6rem 0; color: #334155; }}
    .summary ul, .summary ol {{ margin: 0.5rem 0 0.5rem 1.4rem; color: #334155; }}
    .summary li {{ margin: 0.3rem 0; }}
    .summary strong {{ color: #1e293b; font-weight: 600; }}
    .summary a {{ color: #1a6b8a; text-decoration: none; }}
    .summary a:hover {{ text-decoration: underline; }}

    /* ── Section callouts ── */
    .changes-callout {{
      background: #fffbeb;
      border-left: 4px solid #d97706;
      border-radius: 0 8px 8px 0;
      padding: 1rem 1.25rem;
      margin-top: 0.6rem;
    }}
    .changes-callout p, .changes-callout li {{ color: #451a03; }}
    .changes-callout a {{ color: #92400e; }}

    .comments-callout {{
      background: #f0fdf4;
      border-left: 4px solid #16a34a;
      border-radius: 0 8px 8px 0;
      padding: 1rem 1.25rem;
      margin-top: 0.6rem;
    }}
    .comments-callout p, .comments-callout li {{ color: #052e16; }}
    .comments-callout a {{ color: #166534; }}

    /* ── Footer ── */
    .page-footer {{
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid #e2e8f0;
      font-size: 0.82rem;
      color: #94a3b8;
      line-height: 1.8;
    }}
    .page-footer a {{ color: #1a6b8a; text-decoration: none; }}
    .page-footer a:hover {{ text-decoration: underline; }}

    /* ── Public comment notice ── */
    .public-comment-notice {{
      background: #fef9c3;
      border-left: 4px solid #ca8a04;
      border-radius: 0 6px 6px 0;
      padding: 0.6rem 1rem;
      font-size: 0.9rem;
      color: #713f12;
      margin-bottom: 1.5rem;
    }}

    @media (max-width: 600px) {{
      .summary h2 {{ font-size: 1.25rem; }}
      .page {{ padding: 1.25rem 1rem 3rem; }}
    }}
  </style>
</head>
<body>

  <header class="site-header">
    <div class="header-inner">
      <h1>⚓ Sausalito City Council: Meeting Day Summary</h1>
      <p class="meeting-label">{subtitle_date}Changes &amp; Public Comments</p>
    </div>
  </header>

  <div class="pdf-bar">
    <div class="pdf-bar-inner">
      <span>📄 <a href="{source_url}">{pdf_label}</a></span>
      <span>← <a href="index.html">Initial Agenda Summary</a></span>
    </div>
  </div>

  <div class="page">
    <div class="summary">

      {(f'<div class="public-comment-notice">📣 Open session starts at <strong>{public_comment_time}</strong></div>') if public_comment_time else ""}

      <h2>📝 Changes to the Agenda</h2>
      <div class="changes-callout">
        {changes_html}
      </div>

      <h2>💬 Public Comments</h2>
      <div class="comments-callout">
        {comments_html}
      </div>

    </div>

    <footer class="page-footer">
      <p>Last updated: {updated_str}</p>
      <p>Contact: <a href="mailto:jorisvanmens@gmail.com">jorisvanmens@gmail.com</a></p>
    </footer>
  </div>

</body>
</html>
"""
    FINAL_HTML_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    FINAL_HTML_OUTPUT_PATH.write_text(html, encoding="utf-8")
    return FINAL_HTML_OUTPUT_PATH


def run_final_mode(args) -> None:
    """
    Final-agenda pipeline: fetch the meeting-day PDF, diff against initial,
    summarize public comments from new links, write final.html, send email.
    """
    # Load stored event_id
    last_event_id = get_last_event_id()
    if not last_event_id:
        print("No stored event_id — run the initial mode first.", file=sys.stderr)
        sys.exit(1)

    # Load initial PDF
    initial_pdf_path = AGENDAS_DIR / f"event_{last_event_id}_initial.pdf"
    if not initial_pdf_path.exists():
        print(f"Initial PDF not found: {initial_pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Skip if already processed (prevents double-run at DST boundary when both crons fire)
    existing_final = AGENDAS_DIR / f"event_{last_event_id}_final.pdf"
    if not args.force and not args.use_stored_final_pdf and existing_final.exists():
        print(f"Final summary already generated ({existing_final.name}). Pass --force to re-run.")
        sys.exit(0)

    print(f"Loading initial PDF: {initial_pdf_path.name}")
    initial_pdf_bytes = initial_pdf_path.read_bytes()
    initial_title, initial_text = _parse_pdf(initial_pdf_bytes)
    initial_links = set(_pdf_links_from_bytes(initial_pdf_bytes))
    meeting_date = extract_meeting_date(initial_text)
    agenda_url = AGENDA_URL_TEMPLATE.format(event_id=last_event_id)
    meeting_dt = extract_meeting_datetime(initial_text)
    public_comment_time = meeting_dt.strftime("%-I:%M %p %Z") if meeting_dt else ""
    print(f"  event_id : {last_event_id}")
    print(f"  meeting  : {initial_title}")
    print(f"  date     : {meeting_date or '(not found)'}")
    print(f"  time     : {public_comment_time or '(not found)'}\n")

    # Timing check
    if not args.skip_timing_check:
        if meeting_dt is None:
            print("Could not parse meeting date/time from initial PDF.", file=sys.stderr)
            print("Pass --skip-timing-check to proceed regardless.", file=sys.stderr)
            sys.exit(0)
        hours_until = (meeting_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 3600
        if not is_within_prefetch_window(meeting_dt):
            if hours_until < 0:
                print(f"Meeting already started {-hours_until:.1f}h ago. Exiting.")
            else:
                print(f"Meeting is {hours_until:.1f}h away — outside the 5-hour prefetch window. Exiting.")
            sys.exit(0)
        print(f"Meeting is {hours_until:.1f}h away — within prefetch window.\n")

    # Fetch or load final PDF
    if args.use_stored_final_pdf:
        final_pdf_path = AGENDAS_DIR / f"event_{last_event_id}_final.pdf"
        if not final_pdf_path.exists():
            print(f"Final PDF not found: {final_pdf_path}", file=sys.stderr)
            print("Run without --use-stored-final-pdf to fetch it first.", file=sys.stderr)
            sys.exit(1)
        final_pdf_bytes = final_pdf_path.read_bytes()
        _, final_text = _parse_pdf(final_pdf_bytes)
        source_url = agenda_url
        print(f"Loaded stored final PDF: {final_pdf_path.name}\n")
    else:
        print("Fetching final agenda...")
        try:
            _, final_text, source_url, final_pdf_bytes = fetch_agenda_text(agenda_url)
        except requests.HTTPError as exc:
            print(f"HTTP error fetching final agenda: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"Error fetching final agenda: {exc}", file=sys.stderr)
            sys.exit(1)
        if final_pdf_bytes:
            final_pdf_path = AGENDAS_DIR / f"event_{last_event_id}_final.pdf"
            AGENDAS_DIR.mkdir(exist_ok=True)
            final_pdf_path.write_bytes(final_pdf_bytes)
            print(f"Final PDF saved: {final_pdf_path.name}\n")

    if args.fetch_final_only:
        print("Stopping after PDF fetch (--fetch-final-only).")
        return

    # Link diff: find URLs in final PDF that weren't in the initial PDF
    final_links = _pdf_links_from_bytes(final_pdf_bytes) if final_pdf_bytes else []
    new_links = [l for l in final_links if l not in initial_links]
    print(f"Links: {len(initial_links)} initial, {len(final_links)} final, {len(new_links)} new")
    for link in new_links:
        print(f"  + {link}")
    print()

    # Step 1: Summarize changes
    print("Summarizing agenda changes with Claude...")
    try:
        changes_summary = summarize_agenda_changes(initial_text, final_text, initial_title, agenda_url)
    except Exception as exc:
        print(f"Error summarizing changes: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Done.\n")

    # Step 2: Fetch new linked documents and summarize as public comments
    comment_docs: list[tuple[str, str]] = []
    if new_links:
        print(f"Fetching {len(new_links)} new linked document(s)...")
        for url in new_links:
            print(f"  → {url}")
            try:
                text = fetch_linked_document(url)
                if text.strip():
                    comment_docs.append((url, text))
                    print(f"     {len(text):,} chars extracted")
            except Exception as exc:
                print(f"     failed: {exc}", file=sys.stderr)
        print()

    print(f"Summarizing {len(comment_docs)} document(s) with Claude...")
    try:
        comments_summary = summarize_public_comments(comment_docs, initial_title)
    except Exception as exc:
        print(f"Error summarizing comments: {exc}", file=sys.stderr)
        comments_summary = "Error occurred while summarizing public comments."
    print("Done.\n")

    # Step 3: Write HTML
    html_path = write_final_html(
        initial_title, changes_summary, comments_summary,
        agenda_url, source_url, meeting_date, public_comment_time,
    )
    print(f"Final HTML written to: {html_path}\n")

    # Step 4: Send email
    if args.skip_email:
        print("Email: skipped (--skip-email)")
    else:
        now_utc = datetime.now(timezone.utc)
        updated_str = now_utc.strftime("%-d %B %Y at %-I:%M %p UTC")
        subject = "Today's meeting: comments & agenda update"
        combined_markdown = (
            "# Changes to the Agenda\n\n" + changes_summary
            + "\n\n---\n\n# Public Comments\n\n" + comments_summary
        )
        try:
            email_html = _build_email_body(combined_markdown, source_url, meeting_date, updated_str)
            send_email(subject, email_html)
        except Exception as exc:
            print(f"Email: unexpected error — {exc}", file=sys.stderr)


def send_email(subject: str, html_body: str) -> None:
    """
    Send the agenda summary as an HTML email via SendGrid.
    Silently skips if SENDGRID_API_KEY is not set.
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        print("Email: SENDGRID_API_KEY not set — skipping.")
        return

    recipients = get_recipients()
    if not recipients:
        print("Email: EMAIL_RECIPIENTS not set — skipping.")
        return

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Email as SgEmail
    from sendgrid.helpers.mail import Mail

    sender_name, sender_addr = SENDER_EMAIL
    print(f"Email: sending to {len(recipients)} recipient(s)")
    print(f"  From   : {sender_name} <{sender_addr}>")
    print(f"  To     : {', '.join(recipients)}")
    print(f"  Subject: {subject}")

    try:
        message = Mail(
            from_email=SgEmail(sender_addr, sender_name),
            to_emails=recipients,
            subject=subject,
            html_content=html_body,
        )
        response = SendGridAPIClient(api_key).send(message)
        print(f"Email: sent successfully (HTTP {response.status_code})")
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if status == 403:
            print(
                f"Email: SendGrid 403 Forbidden — '{sender_addr}' may not be verified.\n"
                "Fix: SendGrid dashboard → Settings → Sender Authentication.",
                file=sys.stderr,
            )
        elif status:
            print(f"Email: SendGrid error {status} — {body or exc}", file=sys.stderr)
        else:
            print(f"Email: failed to send — {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize the next Sausalito City Council meeting agenda, "
            "highlighting cycling, pedestrian safety, and housing topics."
        )
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        help=(
            "Skip auto-discovery and use this agenda URL directly. Example:\n"
            "  --url 'https://sausalito.granicus.com/AgendaViewer.php?view_id=6&event_id=2791'"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="(Initial mode) Re-run even if the event_id has not changed.",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Run the full workflow but do not send an email.",
    )
    parser.add_argument(
        "--use-stored-pdf",
        action="store_true",
        help=(
            "(Initial mode) Skip fetching; use the most recently saved "
            "event_*_initial.pdf. Implies --force."
        ),
    )
    # ── Final-mode flags ──────────────────────────────────────────────────────
    parser.add_argument(
        "--final",
        action="store_true",
        help=(
            "Run the final-agenda pipeline: fetch meeting-day PDF, diff against "
            "initial, summarize new linked documents as public comments, write "
            "final.html, and email."
        ),
    )
    parser.add_argument(
        "--skip-timing-check",
        action="store_true",
        help=(
            "(Final mode) Skip the check that the meeting is within the "
            "3-hour prefetch window."
        ),
    )
    parser.add_argument(
        "--fetch-final-only",
        action="store_true",
        help="(Final mode) Only fetch and save the final PDF; skip all summarization.",
    )
    parser.add_argument(
        "--use-stored-final-pdf",
        action="store_true",
        help="(Final mode) Use the stored event_*_final.pdf instead of fetching.",
    )
    args = parser.parse_args()

    if args.final:
        run_final_mode(args)
        return

    # ── Steps 1 & 2: Resolve agenda source ───────────────────────────────────
    if args.use_stored_pdf:
        try:
            meeting_title, agenda_text, source_url, current_event_id, pdf_bytes = load_stored_pdf()
        except Exception as exc:
            print(f"Error loading stored PDF: {exc}", file=sys.stderr)
            sys.exit(1)
        agenda_url = source_url
        save_pdf = False
        print(f"Loaded stored PDF (event_id={current_event_id or 'unknown'})\n")
    else:
        # ── Step 1: Resolve the agenda URL ───────────────────────────────────
        if args.url:
            agenda_url = args.url
            print(f"Using provided URL:\n  {agenda_url}\n")
        else:
            try:
                agenda_url = find_next_agenda_url()
                print(f"Found agenda:\n  {agenda_url}\n")
            except NoAgendaPublishedError as exc:
                print(f"{exc}")
                sys.exit(0)
            except Exception as exc:
                print(f"Error finding agenda URL: {exc}", file=sys.stderr)
                print(
                    "\nTip: pass --url to bypass auto-discovery, e.g.:\n"
                    "  python city_council_agenda.py "
                    "--url 'https://sausalito.granicus.com/AgendaViewer.php?view_id=6&event_id=2791'",
                    file=sys.stderr,
                )
                sys.exit(1)

        # ── Step 2: Fetch and parse the agenda ───────────────────────────────
        try:
            meeting_title, agenda_text, source_url, pdf_bytes = fetch_agenda_text(agenda_url)
        except requests.HTTPError as exc:
            print(f"HTTP error fetching agenda ({exc.response.status_code}): {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"Error fetching agenda: {exc}", file=sys.stderr)
            sys.exit(1)

        # ── Check whether this agenda is new ─────────────────────────────────
        event_id_match = re.search(r"event_id=(\d+)", agenda_url)
        current_event_id = event_id_match.group(1) if event_id_match else ""
        last_event_id = get_last_event_id()

        if not args.force and current_event_id and current_event_id == last_event_id:
            print(f"Agenda event_id={current_event_id} was already processed. Nothing to do.")
            print("Pass --force to re-run anyway.")
            sys.exit(0)

        print(f"New agenda detected (event_id={current_event_id}, last={last_event_id or 'none'}).\n")
        save_pdf = bool(pdf_bytes)

    meeting_date = extract_meeting_date(agenda_text)
    print(f"Meeting : {meeting_title}")
    print(f"Date    : {meeting_date or '(not found)'}")
    print(f"Source  : {source_url}")
    print(f"Agenda  : {len(agenda_text):,} characters extracted\n")

    # ── Step 3: Summarize with Claude ────────────────────────────────────────
    print("Summarizing with Claude...\n")
    try:
        summary = summarize_agenda(meeting_title, agenda_text, agenda_url)
    except anthropic.AuthenticationError:
        print(
            "Authentication error: ANTHROPIC_API_KEY is missing or invalid.\n"
            "Set it with: export ANTHROPIC_API_KEY='sk-ant-...'",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Error calling Claude API: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Step 4: Write HTML and save state ────────────────────────────────────
    html_path = write_html(meeting_title, summary, agenda_url, source_url, meeting_date)
    print(f"HTML written to: {html_path}\n")
    if current_event_id and not args.use_stored_pdf:
        save_event_id(current_event_id)
        print(f"Saved event_id={current_event_id} to {LAST_EVENT_ID_PATH}\n")
    if save_pdf:
        pdf_path = save_agenda_pdf(current_event_id, pdf_bytes)
        print(f"Agenda PDF saved to: {pdf_path}\n")

    # ── Step 5: Send email ────────────────────────────────────────────────────
    if args.skip_email:
        print("Email: skipped (--skip-email)")
    else:
        now_utc = datetime.now(timezone.utc)
        updated_str = now_utc.strftime("%-d %B %Y at %-I:%M %p UTC")
        subject = (
            f"Sausalito City Council — {meeting_date + ' ' if meeting_date else ''}Meeting Agenda Summary"
        )
        try:
            email_html = _build_email_body(summary, source_url, meeting_date, updated_str)
            send_email(subject, email_html)
        except Exception as exc:
            print(f"Email: unexpected error — {exc}", file=sys.stderr)

    # ── Step 6: Print to stdout ───────────────────────────────────────────────
    print("=" * 60)
    print("  SAUSALITO CITY COUNCIL — AGENDA SUMMARY")
    print("=" * 60)
    print(summary)
    print()


if __name__ == "__main__":
    main()
