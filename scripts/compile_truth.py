"""
Generate compiled-truth.md from wiki articles using priority scoring.

Instead of concatenating all articles alphabetically (which gets truncated),
this scores each article by recency, cross-linkedness, access frequency,
and pinned status — then fills a character budget from highest-scored down.

Zero LLM cost — pure file I/O and Python scoring.

Usage:
    uv run python scripts/compile_truth.py              # default 40K char budget
    uv run python scripts/compile_truth.py --budget 60000
    uv run python scripts/compile_truth.py --verbose     # show scoring breakdown
    uv run python scripts/compile_truth.py --all         # ignore budget, include everything
"""

from __future__ import annotations

import argparse
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

from config import KNOWLEDGE_DIR, CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR
from utils import count_inbound_links, extract_wikilinks, list_wiki_articles, load_state


COMPILED_TRUTH_FILE = KNOWLEDGE_DIR / "compiled-truth.md"
DEFAULT_BUDGET_CHARS = 40_000

# ── Scoring weights ──────────────────────────────────────────────────
# These control how articles are ranked for inclusion in compiled truth.
# Pinned articles bypass scoring entirely (always included first).
WEIGHT_RECENCY = 0.40     # recently updated articles score higher
WEIGHT_LINKEDNESS = 0.35  # more cross-linked articles are more foundational
WEIGHT_ACCESS = 0.25      # frequently queried articles are more useful


def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter fields from article content.

    Returns a dict with parsed fields. Handles the common frontmatter
    fields: updated, created, pinned, title, tags, aliases.
    """
    if not content.startswith("---"):
        return {}

    end = content.find("---", 3)
    if end == -1:
        return {}

    fm_text = content[3:end].strip()
    result: dict = {}

    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" not in line or line.startswith("-") or line.startswith("#"):
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key == "pinned":
            result["pinned"] = value.lower() in ("true", "yes", "1")
        elif key in ("updated", "created"):
            result[key] = value
        elif key == "title":
            result["title"] = value

    return result


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- delimited) from article content."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()
    return content.strip()


def extract_truth_section(content: str) -> str | None:
    """Extract the ## Truth section from a new-format article.

    Returns everything between '## Truth' and the next '---' horizontal rule
    or '## Timeline' header, whichever comes first. Returns None if no
    ## Truth header found.
    """
    match = re.search(r"^## Truth\s*\n", content, re.MULTILINE)
    if not match:
        return None

    start = match.end()
    boundary = re.search(r"^(?:---|\#\# Timeline)\s*$", content[start:], re.MULTILINE)
    if boundary:
        truth = content[start:start + boundary.start()]
    else:
        truth = content[start:]

    return truth.strip()


def extract_fallback_truth(content: str) -> str:
    """Extract truth from old-format articles (no ## Truth header).

    Fallback strategy:
    1. Intro paragraph (text before first ## heading)
    2. ## Key Points section
    3. ## Related Concepts section
    If no Key Points, use first 200 words of ## Details instead.
    """
    body = strip_frontmatter(content)

    parts: list[str] = []

    # 1. Intro paragraph — everything before the first ## heading
    lines = body.split("\n")
    intro_lines: list[str] = []
    for line in lines:
        if line.startswith("# ") and not intro_lines:
            continue  # skip title
        if line.startswith("## "):
            break
        intro_lines.append(line)
    intro = "\n".join(intro_lines).strip()
    if not intro:
        # Connection articles: "## The Connection" serves as the intro
        intro = extract_section(body, "The Connection") or ""
    if intro:
        parts.append(intro)

    # 2. Key Points / Key Insight section
    key_points = extract_section(body, "Key Points")
    if not key_points:
        key_points = extract_section(body, "Key Insight")
    if key_points:
        parts.append(f"### Key Points\n\n{key_points}")
    else:
        details = extract_section(body, "Details")
        if not details:
            details = extract_section(body, "Evidence")
        if details:
            words = details.split()
            truncated = " ".join(words[:200])
            if len(words) > 200:
                truncated += "..."
            parts.append(f"### Details (excerpt)\n\n{truncated}")

    # 3. Related Concepts (look for both ## and ### levels)
    related = extract_section(body, "Related Concepts")
    if related:
        parts.append(f"### Related Concepts\n\n{related}")

    return "\n\n".join(parts)


def extract_section(body: str, heading: str) -> str | None:
    """Extract content under a ## or ### heading, up to the next same-or-higher heading."""
    pattern = rf"^#{{2,3}}\s+{re.escape(heading)}\s*\n"
    match = re.search(pattern, body, re.MULTILINE)
    if not match:
        return None

    start = match.end()
    level = match.group().count("#")

    next_heading = re.search(
        rf"^#{{{1},{level}}}\s+\S",
        body[start:],
        re.MULTILINE,
    )
    if next_heading:
        section = body[start:start + next_heading.start()]
    else:
        section = body[start:]

    return section.strip() or None


# ── Scoring ──────────────────────────────────────────────────────────

def score_recency(updated: str | None, today: date) -> float:
    """Score based on how recently the article was updated.

    Uses exponential decay: today = 1.0, 7 days ago ≈ 0.74, 30 days ≈ 0.40, 90 days ≈ 0.18.
    """
    if not updated:
        return 0.1  # unknown date gets a low baseline

    try:
        updated_date = date.fromisoformat(updated)
    except ValueError:
        return 0.1

    days = max(0, (today - updated_date).days)
    return 1.0 / (1.0 + days * 0.05)


def score_linkedness(inbound_links: int) -> float:
    """Score based on how many other articles link to this one.

    Log scale to prevent hub articles from dominating.
    Normalized so 20 inbound links = 1.0.
    """
    return min(1.0, math.log1p(inbound_links) / math.log1p(20))


def score_access(access_count: int) -> float:
    """Score based on how often the article has been accessed via query.py.

    Log scale, normalized so 50 accesses = 1.0.
    """
    return min(1.0, math.log1p(access_count) / math.log1p(50))


def compute_score(
    recency: float,
    linkedness: float,
    access: float,
) -> float:
    """Weighted combination of scoring signals."""
    return (
        WEIGHT_RECENCY * recency
        + WEIGHT_LINKEDNESS * linkedness
        + WEIGHT_ACCESS * access
    )


# ── Article data ─────────────────────────────────────────────────────

class ScoredArticle:
    """An article with its extracted truth and priority score."""

    __slots__ = (
        "rel_path", "truth", "pinned", "score",
        "recency", "linkedness", "access", "char_count",
    )

    def __init__(
        self,
        rel_path: str,
        truth: str,
        pinned: bool,
        score: float,
        recency: float,
        linkedness: float,
        access: float,
    ):
        self.rel_path = rel_path
        self.truth = truth
        self.pinned = pinned
        self.score = score
        self.recency = recency
        self.linkedness = linkedness
        self.access = access
        # Pre-compute the character cost of including this article
        slug = rel_path.replace(".md", "")
        self.char_count = len(f"\n---\n\n## {slug}\n\n{truth}\n")


# ── Main compilation ─────────────────────────────────────────────────

def build_inbound_link_map() -> dict[str, int]:
    """Build a map of article slug → inbound link count.

    Scans all articles once and counts how many other articles link to each.
    More efficient than calling count_inbound_links() per article.
    """
    link_counts: dict[str, int] = {}

    all_articles = list_wiki_articles()
    for article in all_articles:
        content = article.read_text(encoding="utf-8")
        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue
            link_counts[link] = link_counts.get(link, 0) + 1

    return link_counts


def compile_truth(
    budget: int = DEFAULT_BUDGET_CHARS,
    include_all: bool = False,
    verbose: bool = False,
) -> tuple[int, int, int]:
    """Generate compiled-truth.md with priority scoring.

    Returns (included_count, total_count, pinned_count).
    """
    today = date.today()
    state = load_state()
    access_counts = state.get("access_counts", {})
    inbound_map = build_inbound_link_map()

    # ── Extract and score all articles ────────────────────────────────
    articles: list[ScoredArticle] = []

    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            rel = str(md_file.relative_to(KNOWLEDGE_DIR)).replace("\\", "/")
            slug = rel.replace(".md", "")

            # Extract truth content
            truth = extract_truth_section(content)
            if not truth:
                truth = extract_fallback_truth(content)
            if not truth:
                continue

            # Parse frontmatter for metadata
            fm = parse_frontmatter(content)
            pinned = fm.get("pinned", False)
            updated = fm.get("updated") or fm.get("created")

            # Compute scoring signals
            rec = score_recency(updated, today)
            lnk = score_linkedness(inbound_map.get(slug, 0))
            acc = score_access(access_counts.get(slug, 0))
            total_score = compute_score(rec, lnk, acc)

            articles.append(ScoredArticle(
                rel_path=rel,
                truth=truth,
                pinned=pinned,
                score=total_score,
                recency=rec,
                linkedness=lnk,
                access=acc,
            ))

    total_count = len(articles)

    # ── Select articles by priority ───────────────────────────────────
    pinned_articles = [a for a in articles if a.pinned]
    unpinned_articles = [a for a in articles if not a.pinned]
    unpinned_articles.sort(key=lambda a: a.score, reverse=True)

    selected: list[ScoredArticle] = []
    used_chars = 0

    if include_all:
        # --all flag: include everything, no budget
        selected = pinned_articles + unpinned_articles
    else:
        # 1. Pinned articles always go in first
        for article in pinned_articles:
            selected.append(article)
            used_chars += article.char_count

        # 2. Fill remaining budget with highest-scored articles
        for article in unpinned_articles:
            if used_chars + article.char_count > budget:
                continue  # skip articles that would bust the budget
            selected.append(article)
            used_chars += article.char_count

    pinned_count = len(pinned_articles)
    included_count = len(selected)

    # ── Verbose output ────────────────────────────────────────────────
    if verbose:
        print(f"\n{'-' * 70}")
        print(f"  Priority Scoring Breakdown ({total_count} articles)")
        print(f"  Budget: {budget:,} chars | Pinned: {pinned_count}")
        print(f"{'-' * 70}")
        print(f"  {'#':>3}  {'Score':>5}  {'R':>4}  {'L':>4}  {'A':>4}  {'Pin':>3}  {'Chars':>6}  Article")
        print(f"  {'---':>3}  {'-----':>5}  {'----':>4}  {'----':>4}  {'----':>4}  {'---':>3}  {'------':>6}  {'-----'}")

        # Show all articles sorted by score (pinned first)
        all_sorted = pinned_articles + unpinned_articles
        selected_set = set(id(a) for a in selected)
        for i, article in enumerate(all_sorted, 1):
            is_included = id(article) in selected_set
            marker = "+" if is_included else " "
            pin = "PIN" if article.pinned else "   "
            slug = article.rel_path.replace(".md", "")
            print(
                f"  {marker}{i:>2}  {article.score:.3f}  "
                f"{article.recency:.2f}  {article.linkedness:.2f}  {article.access:.2f}  "
                f"{pin}  {article.char_count:>5}  {slug}"
            )

        print(f"{'-' * 70}")
        excluded = total_count - included_count
        print(f"  Included: {included_count} | Excluded: {excluded} | Used: {used_chars:,} / {budget:,} chars")
        print(f"{'-' * 70}\n")

    # ── Write compiled-truth.md ───────────────────────────────────────
    now = datetime.now(timezone.utc).astimezone()
    timestamp = now.isoformat(timespec="seconds")

    budget_note = "all" if include_all else f"budget {budget:,} chars"
    lines = [
        "# Compiled Truth",
        "",
        f"> {included_count} articles (of {total_count} total, {budget_note}) | Generated {timestamp}",
    ]

    for article in selected:
        slug = article.rel_path.replace(".md", "")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"## {slug}")
        lines.append("")
        lines.append(article.truth)

    lines.append("")

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    COMPILED_TRUTH_FILE.write_text("\n".join(lines), encoding="utf-8")

    return included_count, total_count, pinned_count


def main():
    parser = argparse.ArgumentParser(
        description="Generate compiled-truth.md with priority-scored article selection"
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET_CHARS,
        help=f"Character budget for compiled truth (default: {DEFAULT_BUDGET_CHARS:,})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help="Include all articles regardless of budget (for debugging)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show scoring breakdown for all articles",
    )
    args = parser.parse_args()

    included, total, pinned = compile_truth(
        budget=args.budget,
        include_all=args.include_all,
        verbose=args.verbose,
    )

    excluded = total - included
    print(f"Compiled truth: {included}/{total} articles included ({pinned} pinned, {excluded} excluded)")
    print(f"Written to: {COMPILED_TRUTH_FILE}")


if __name__ == "__main__":
    main()
