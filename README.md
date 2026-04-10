# Claude Context Engine

**Your AI conversations + project docs compile themselves into a searchable knowledge base.**

Adapted from [Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture. Unlike the original which clips web articles, this system ingests **two sources**: your Claude Code conversations (captured automatically via hooks) and your project's static files -- design specs, architecture docs, governance rules, auto-memories, and any external articles you drop into the sources folder. The [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) extracts decisions, lessons learned, patterns, and gotchas, then compiles them into structured, cross-referenced knowledge articles. Retrieval uses a simple index file instead of RAG -- no vector database, no embeddings, just markdown.

Forked from [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) and significantly extended.

## What's New vs. Upstream

| Feature | Upstream | This Fork |
|---------|----------|-----------|
| **Source ingestion** | Conversations only | Conversations + external files via `sources.yaml` |
| **Handler system** | None | Pluggable handlers (markdown built-in, PDF/URL planned) |
| **Resume-here state** | None | `wip.md` extracted from sessions, injected on start |
| **Drop zone** | None | `sources/articles/`, `sources/notes/`, `sources/pdfs/` |
| **Compiled Truth** | None | Zero-cost `compiled-truth.md` — all facts in one file, included in every prompt |
| **Truth + Timeline format** | Single-zone articles | Articles split into Truth (facts) and Timeline (provenance) |
| **Priority-scored truth** | None | Compiled truth uses recency, cross-linking, and access frequency to select top articles within a character budget |
| **O(1) prompt cost** | O(n) — all articles dumped into prompt | Index + compiled truth — fixed cost regardless of KB size |
| **Knowledge location** | Inside `.claude/` | Project root (`knowledge/`) -- Claude Code blocks writes inside `.claude/` |
| **Exclude patterns** | Broken for external paths | Fixed (`fnmatch` against filenames) |
| **State schema** | Flat `ingested` dict | Split `ingested_daily` + `ingested_sources` with auto-migration |
| **Permission mode** | `acceptEdits` | `bypassPermissions` (required for unattended ingestion) |
| **Auto-memory ingestion** | None | Symlink `.claude/memory/` and add to `sources.yaml` |

## Architecture

```
                       SESSION LIFECYCLE
                       =================

  ┌─────────────┐     SessionStart hook      ┌──────────────────┐
  │ Claude Code  │◄─── injects index.md ─────│ session-start.py │
  │   session    │     + wip.md + recent log  └──────────────────┘
  └──────┬───────┘
         │ SessionEnd / PreCompact hook
         ▼
  ┌──────────────┐     background spawn      ┌─────────────┐
  │session-end.py│──────────────────────►    │  flush.py   │
  └──────────────┘   (detached process)       └──────┬──────┘
                                                     │
                                  ┌──────────────────┼──────────────────┐
                                  ▼                  ▼                  ▼
                           Agent SDK call    daily/YYYY-MM-DD.md   wip.md
                           (extract knowledge)                   (resume-here)
                                                     │
                                                     │ after 6 PM
                                                     ▼
                                              ┌─────────────┐
                                              │ compile.py  │──► knowledge/
                                              └──────┬──────┘
                                                     │
                                                     ▼
                                              ┌──────────────────┐
                                              │compile_truth.py  │──► compiled-truth.md
                                              └──────────────────┘    (zero cost)


                       SOURCE INGESTION
                       ================

  sources.yaml ──► ingest.py ──► source_handlers/ ──► Agent SDK ──► knowledge/
       │                              │
       ├── design-specs/*.md          ├── markdown.py (built-in)
       ├── implementation-plans/*.md  ├── pdf.py      (planned)
       ├── governance (CLAUDE.md)     └── url.py      (planned)
       ├── captured-memory/*.md
       └── sources/articles/*.md
```

## Quick Start

### 1. Fork and clone

```bash
git clone https://github.com/YOUR_USER/claude-context-engine .claude/memory-compiler
cd .claude/memory-compiler
uv sync
```

### 2. Configure hooks

Merge into your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run --directory .claude/memory-compiler python .claude/memory-compiler/hooks/session-start.py",
        "timeout": 15
      }]
    }],
    "PreCompact": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run --directory .claude/memory-compiler python .claude/memory-compiler/hooks/session-end.py",
        "timeout": 10
      }]
    }],
    "SessionEnd": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run --directory .claude/memory-compiler python .claude/memory-compiler/hooks/session-end.py",
        "timeout": 10
      }]
    }]
  }
}
```

### 3. Set up source ingestion

```bash
cp sources.yaml.example sources.yaml
# Edit sources.yaml: point globs at your project's docs, specs, memories
uv run python scripts/ingest.py --dry-run   # preview
uv run python scripts/ingest.py             # run
```

### 4. Use it

Sessions accumulate automatically. After 6 PM, compilation triggers on next flush. Knowledge base grows with every session and every `ingest.py` run.

## How It Works

```
Conversation -> SessionEnd/PreCompact hooks -> flush.py extracts knowledge
    -> daily/YYYY-MM-DD.md -> compile.py -> knowledge/concepts/, connections/
        -> compile_truth.py -> compiled-truth.md (zero cost)
            -> SessionStart hook injects compiled truth + index + wip.md -> cycle repeats

Source files -> sources.yaml -> ingest.py -> knowledge/concepts/, connections/
    -> compile_truth.py -> compiled-truth.md (zero cost)
        -> same index, same articles, cross-linked with session knowledge
```

- **Hooks** capture conversations automatically (session end + pre-compaction safety net)
- **flush.py** extracts knowledge via Agent SDK, writes daily log + wip.md resume-here state
- **compile.py** turns daily logs into organized concept articles with cross-references
- **ingest.py** turns source files (specs, docs, memories, articles) into the same knowledge base
- **query.py** answers questions using index-guided retrieval (no RAG needed at personal scale)
- **lint.py** runs 9 health checks (broken links, orphans, contradictions, staleness, uningested sources, low-priority articles)

## Key Commands

```bash
# Session logs -> knowledge
uv run python scripts/compile.py                    # compile new daily logs
uv run python scripts/compile.py --all              # force recompile

# Source files -> knowledge
uv run python scripts/ingest.py                     # ingest new/changed sources
uv run python scripts/ingest.py --all               # force re-ingest everything
uv run python scripts/ingest.py --source design-specs  # one group only
uv run python scripts/ingest.py --dry-run --verbose

# Ask the knowledge base
uv run python scripts/query.py "question"
uv run python scripts/query.py "question" --file-back  # save answer as article

# Health checks
uv run python scripts/lint.py                       # all checks
uv run python scripts/lint.py --structural-only     # free structural checks only

# Generate compiled truth (zero cost, pure Python)
uv run python scripts/compile_truth.py              # regenerate with priority scoring
uv run python scripts/compile_truth.py --verbose    # show scoring breakdown
uv run python scripts/compile_truth.py --budget 60000  # custom char budget
uv run python scripts/compile_truth.py --all        # include everything (ignore budget)
```

## Configuration: sources.yaml

```yaml
version: 1

sources:
  - id: design-specs           # Unique identifier
    type: markdown             # Handler type (markdown now; pdf, url planned)
    include:                   # Globs relative to memory-compiler root
      - "../../docs/specs/*.md"
    exclude:                   # Filename patterns (fnmatch)
      - "**/*DRAFT.md"
    category: design-specs     # Tag on generated articles
    description: "Project design specs"

  - id: external-articles
    type: markdown
    include:
      - "sources/articles/*.md"   # Drop zone
      - "sources/notes/*.md"
    category: external
    description: "Web clippings, research notes"
```

### Adding a handler

1. Create `scripts/source_handlers/your_type.py`
2. Define `extract(path: Path) -> SourceDocument`
3. Call `register("your_type", extract)`
4. Import in `scripts/source_handlers/__init__.py`

## Portability

Each project gets its own clone with its own `sources.yaml` and isolated `knowledge/` directory:

1. Clone into `.claude/memory-compiler/`
2. `uv sync`
3. Copy `sources.yaml.example` -> `sources.yaml`, customize globs
4. Merge hooks into `.claude/settings.json`
5. `uv run python scripts/ingest.py`

The `knowledge/` output lives at the **project root** (not inside `.claude/`) because Claude Code blocks Agent SDK writes inside `.claude/`.

## Memory Symlink

Claude Code stores auto-memory outside the project. To ingest it:

```bash
# Windows
mklink /J .claude\memory %USERPROFILE%\.claude\projects\<slug>\memory

# Mac/Linux
ln -s ~/.claude/projects/<slug>/memory .claude/memory
```

Then add a `captured-memory` source group pointing at `../../.claude/memory/*.md`.

## Why No RAG?

Karpathy's insight: at personal scale (50-500 articles), the LLM reading a structured `index.md` outperforms vector similarity. The LLM understands what you're really asking; cosine similarity just finds similar words. RAG becomes necessary at ~2,000+ articles when the index exceeds the context window.

## Cost

All operations use the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) on your existing Claude subscription (Max, Team, or Enterprise). No separate API key needed.

### Per-operation costs

| Operation | Cost | When it runs |
|-----------|------|-------------|
| Session flush (`flush.py`) | ~$0.01-0.05 | Every session end (automatic) |
| Daily compilation (`compile.py`) | ~$0.30-0.80 | Once per day after 6 PM (automatic) |
| Source ingestion (`ingest.py`) | ~$0.30-0.80/file | Manual only — you control when |
| Compiled truth generation | **$0.00** | After every compile/ingest (pure Python) |
| Structural lint | **$0.00** | Manual — pure Python checks |
| Full lint (with contradictions) | ~$0.15-0.25 | Manual — uses LLM for contradiction detection |
| Query | ~$0.15-0.40 | Manual |

### Daily cost estimate

With typical usage (10-15 coding sessions per day):

| Component | Cost/day |
|-----------|----------|
| Session flushes (10-15 × ~$0.02) | $0.10-0.30 |
| Daily compilation (1×) | $0.30-0.80 |
| **Total automatic cost** | **$0.40-1.10/day** |

Source ingestion is manual — you choose when to run it and how many files to process.

### Why costs are stable

The upstream `claude-memory-compiler` has a design flaw: every compile/ingest call dumps **all** existing wiki articles into the prompt. As the knowledge base grows, costs grow linearly — at 71 articles, a single ingestion cost $1.33-4.53 per file.

This fork fixes that. The prompt includes:
1. **Wiki index** (~one line per article) — grows slowly
2. **Compiled truth** (~150 words per article) — much smaller than full articles (~800+ words each)
3. **Tool access** (Read/Grep) — agent fetches specific articles on demand

Cost per operation is approximately constant regardless of knowledge base size.

## Compiled Truth

Instead of dumping all articles into every prompt (expensive, O(n)) or using embeddings/RAG (complex), this fork generates a zero-cost **compiled truth** file with priority-scored article selection.

### Three-Level Retrieval

| Level | What | Size | Cost |
|-------|------|------|------|
| **Level 0: Map** | `index.md` — article slug + one-line description | ~8K chars | Always injected |
| **Level 1: Essential Truth** | `compiled-truth.md` — top articles by priority score | ~40K chars (configurable) | Always injected |
| **Level 2: Full Articles** | Individual articles via `query.py` or direct Read | On-demand | Per-query |

### Priority Scoring

`compile_truth.py` scores every article and fills a character budget (default 40K) from the highest-scored down. Pinned articles (with `pinned: true` in frontmatter) are always included first.

**Scoring formula** (non-pinned articles):

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| **Recency** | 40% | Exponential decay from `updated` date — today = 1.0, 30 days = 0.40, 90 days = 0.18 |
| **Linkedness** | 35% | Log-scaled count of inbound `[[wikilinks]]` — more cross-linked = more foundational |
| **Access frequency** | 25% | Log-scaled count of `query.py` citations — frequently consulted = more useful |

The result: the most important, most connected, most recently updated articles are always in context. At 75 articles, the top 14 fit within 40K chars. As the KB grows to 500+, the same budget automatically selects the best subset — no manual curation needed.

Run `compile_truth.py --verbose` to see the full scoring breakdown for every article.

### Article Format

Articles use a **Truth + Timeline** format:
- **Truth** (top): current facts, dense, machine-extractable
- **Timeline** (bottom): provenance — when things were learned, from which sources, why decisions changed

See [AGENTS.md](AGENTS.md) for the full article schema.

## Obsidian Integration

The knowledge base is pure markdown with `[[wikilinks]]`. Point an Obsidian vault at `knowledge/` for graph view, backlinks, and search.

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, source handler API, cross-platform details, and customization options.

## Credits

- Forked from [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) by Cole Medin
- Inspired by [Andrej Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) by Anthropic
