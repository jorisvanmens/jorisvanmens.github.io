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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import markdown as md_lib
import requests
from bs4 import BeautifulSoup

GRANICUS_BASE = "https://sausalito.granicus.com"
PUBLISHER_URL = f"{GRANICUS_BASE}/ViewPublisher.php?view_id=6"
AGENDA_URL_TEMPLATE = f"{GRANICUS_BASE}/AgendaViewer.php?view_id=6&event_id={{event_id}}"

HTML_OUTPUT_PATH = Path(__file__).parent / "city-council" / "index.html"

# Browser-like headers to avoid 503s from servers that block plain bots
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def find_next_agenda_url() -> str:
    """
    Scrape the Granicus publisher listing page to find the most
    recent/upcoming City Council meeting with a posted agenda.
    Returns the full agenda viewer URL.
    """
    print(f"Checking {PUBLISHER_URL} for upcoming meetings...")
    resp = requests.get(PUBLISHER_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    match = re.search(r"event_id=(\d+)", resp.text)
    if not match:
        raise ValueError(
            "No event_id found on the Granicus publisher page. "
            "The page structure may have changed, or no agendas are currently posted.\n"
            "Try passing --url with a direct agenda link instead."
        )

    event_id = match.group(1)
    return AGENDA_URL_TEMPLATE.format(event_id=event_id)


def fetch_agenda_text(agenda_url: str) -> tuple[str, str]:
    """
    Fetch a Granicus AgendaViewer page and extract the readable agenda text.
    Returns (meeting_title, agenda_text).
    """
    print(f"Fetching agenda from:\n  {agenda_url}")
    resp = requests.get(agenda_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip noise
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    # Extract a meaningful meeting title
    title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
    meeting_title = title_tag.get_text(strip=True) if title_tag else "City Council Meeting"

    # Find the main content block; Granicus uses various IDs/classes
    main = (
        soup.find(id=re.compile(r"agenda|content|main", re.I))
        or soup.find("div", class_=re.compile(r"agenda|content|main", re.I))
        or soup.find("body")
    )

    raw_text = main.get_text(separator="\n", strip=True)

    # Collapse runs of blank lines into a single blank line
    agenda_text = re.sub(r"\n{3,}", "\n\n", raw_text)

    return meeting_title, agenda_text


def summarize_agenda(meeting_title: str, agenda_text: str, agenda_url: str) -> str:
    """
    Send the agenda text to Claude and return a structured Markdown summary
    highlighting cycling, pedestrian safety, and housing items.
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
State the meeting date, time, and location. Then write 2–3 sentences summarizing the overall themes or most significant items on the agenda.

## 2. Agenda Items
A concise bullet-point list of all substantive agenda items. Skip purely procedural items (call to order, roll call, approval of prior minutes, adjournment).

## 3. Topics of Interest
Identify every agenda item related to any of the following, even if only tangentially:
- **Cycling** — bike lanes, bicycle infrastructure, bike-share, Caltrans roadway projects, multi-use paths, etc.
- **Pedestrian safety** — sidewalks, crosswalks, traffic calming, speed limits, Vision Zero, school safety zones, ADA accessibility, etc.
- **Housing** — affordable housing, zoning or general plan amendments, development/subdivision approvals, ADUs, density bonuses, inclusionary requirements, housing element updates, etc.

For each relevant item, include: the agenda item number, a brief description, and what action is being requested (vote, first reading, discussion only, public hearing, etc.).

If none of those three topics appear on the agenda, state that clearly.

---
Agenda source: {agenda_url}
Meeting: {meeting_title}

{agenda_text}
"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
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


def write_html(meeting_title: str, summary_markdown: str, agenda_url: str) -> Path:
    """
    Convert the Markdown summary to an HTML page and write it to
    city-council/index.html. Returns the path written.
    """
    content_html = md_lib.markdown(summary_markdown, extensions=["extra"])

    now = datetime.now(timezone.utc)
    updated_str = now.strftime("%-d %B %Y at %-I:%M %p UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sausalito City Council — Agenda Summary</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: #f5f7fa;
      color: #1a2332;
      line-height: 1.65;
    }}

    .page {{
      max-width: 780px;
      margin: 0 auto;
      padding: 2.5rem 1.5rem 4rem;
    }}

    /* ── Header ── */
    .site-header {{
      border-bottom: 3px solid #1d4ed8;
      padding-bottom: 1.25rem;
      margin-bottom: 2rem;
    }}
    .site-header h1 {{
      font-size: 1.6rem;
      font-weight: 700;
      color: #1d4ed8;
      letter-spacing: -0.02em;
    }}
    .site-header .subtitle {{
      font-size: 0.95rem;
      color: #64748b;
      margin-top: 0.2rem;
    }}

    /* ── Summary content ── */
    .summary h2 {{
      font-size: 1.1rem;
      font-weight: 700;
      color: #1e293b;
      margin: 2rem 0 0.6rem;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid #e2e8f0;
    }}

    .summary h2:first-child {{ margin-top: 0; }}

    .summary p {{
      margin: 0.6rem 0;
      color: #334155;
    }}

    .summary ul, .summary ol {{
      margin: 0.5rem 0 0.5rem 1.4rem;
      color: #334155;
    }}

    .summary li {{
      margin: 0.3rem 0;
    }}

    .summary strong {{
      color: #1e293b;
      font-weight: 600;
    }}

    /* ── Topics of Interest callout ── */
    .topics-callout {{
      background: #eff6ff;
      border-left: 4px solid #1d4ed8;
      border-radius: 0 6px 6px 0;
      padding: 1rem 1.25rem;
      margin-top: 0.6rem;
    }}

    .topics-callout p,
    .topics-callout li {{
      color: #1e3a5f;
    }}

    /* ── Footer ── */
    .page-footer {{
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid #e2e8f0;
      font-size: 0.82rem;
      color: #94a3b8;
      line-height: 1.6;
    }}

    .page-footer a {{
      color: #1d4ed8;
      text-decoration: none;
    }}

    .page-footer a:hover {{
      text-decoration: underline;
    }}

    @media (max-width: 600px) {{
      .page {{ padding: 1.5rem 1rem 3rem; }}
      .site-header h1 {{ font-size: 1.3rem; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="site-header">
      <h1>Sausalito City Council</h1>
      <p class="subtitle">AI-generated agenda summary &mdash; cycling, pedestrian safety &amp; housing highlights</p>
    </header>

    <div class="summary" id="summary">
      {content_html}
    </div>

    <script>
      // Wrap the Topics of Interest section content in a callout box
      const headings = document.querySelectorAll('#summary h2');
      headings.forEach(h2 => {{
        if (h2.textContent.includes('Topics of Interest')) {{
          const wrapper = document.createElement('div');
          wrapper.className = 'topics-callout';
          let node = h2.nextSibling;
          const collected = [];
          while (node) {{
            const next = node.nextSibling;
            if (node.nodeType === 1 && node.tagName === 'H2') break;
            collected.push(node);
            node = next;
          }}
          h2.after(wrapper);
          collected.forEach(n => wrapper.appendChild(n));
        }}
      }});
    </script>

    <footer class="page-footer">
      <p>Last updated: {updated_str}</p>
      <p>Source: <a href="{agenda_url}">{agenda_url}</a></p>
      <p>Summary generated by Claude AI. Always verify details with the <a href="{agenda_url}">official agenda</a>.</p>
    </footer>
  </div>
</body>
</html>
"""

    HTML_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    HTML_OUTPUT_PATH.write_text(html, encoding="utf-8")
    return HTML_OUTPUT_PATH


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
    args = parser.parse_args()

    # ── Step 1: Resolve the agenda URL ───────────────────────────────────────
    if args.url:
        agenda_url = args.url
        print(f"Using provided URL:\n  {agenda_url}\n")
    else:
        try:
            agenda_url = find_next_agenda_url()
            print(f"Found agenda:\n  {agenda_url}\n")
        except Exception as exc:
            print(f"Error finding agenda URL: {exc}", file=sys.stderr)
            print(
                "\nTip: pass --url to bypass auto-discovery, e.g.:\n"
                "  python city_council_agenda.py "
                "--url 'https://sausalito.granicus.com/AgendaViewer.php?view_id=6&event_id=2791'",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── Step 2: Fetch and parse the agenda ───────────────────────────────────
    try:
        meeting_title, agenda_text = fetch_agenda_text(agenda_url)
    except requests.HTTPError as exc:
        print(f"HTTP error fetching agenda ({exc.response.status_code}): {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error fetching agenda: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Meeting : {meeting_title}")
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

    # ── Step 4: Write HTML ────────────────────────────────────────────────────
    html_path = write_html(meeting_title, summary, agenda_url)
    print(f"HTML written to: {html_path}\n")

    # ── Step 5: Print to stdout ───────────────────────────────────────────────
    print("=" * 60)
    print("  SAUSALITO CITY COUNCIL — AGENDA SUMMARY")
    print("=" * 60)
    print(summary)
    print()


if __name__ == "__main__":
    main()
