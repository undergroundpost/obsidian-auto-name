# obsidian-auto-name

Renames Obsidian "Untitled" notes nightly using an Ollama-hosted LLM. Each note's body is summarized into a short (default 3-word) descriptive title; the file is renamed and any `[[Untitled X]]` wikilink references in other notes are updated. Empty Untitled notes are deleted by default.

## What it does

For every file in the vault whose basename matches one of the configured `RENAME_PATTERNS` (default: `Untitled.md`, `Untitled <N>.md`, `New Note.md`, `New Note <N>.md`), the script:

1. Reads the note. If the body is below a configurable character threshold and `DELETE_EMPTY_UNTITLED` is true, the file is deleted. Otherwise:
2. Honors a frontmatter opt-out flag (default `auto_name: false`) — notes with this flag are skipped entirely.
3. Resolves a title from one of four sources, in priority order:
   - **Frontmatter `title:` field** — most explicit user intent; used directly. Skipped if the frontmatter title itself looks like an Untitled name (defensive against templating plugins that auto-fill it).
   - **H1 heading** — if the note's first non-blank line is an H1 (`# Like This`), use it as-is. No LLM call.
   - **LLM** — `qwen2.5:7b` returns `{"title": "...", "confidence": 0.0-1.0}` via grammar-constrained JSON schema.
   - **First-line fallback** — if the LLM's confidence is below `CONFIDENCE_THRESHOLD`, fall back to the note's first non-blank line (Notion-style).
4. Sanitizes the result (strips non-alphanumeric characters), trims to `MAX_TITLE_WORDS`, drops trailing stop words like "and"/"or"/"the", and applies the configured casing (preserving acronyms like `RFQ`, `VESC`, `iOS`).
5. Applies the optional `TITLE_TEMPLATE` (`{title}` and `{date}` substitutions).
6. Caps the final filename at `MAX_FILENAME_CHARS` (default 50), truncating at the last word boundary.
7. Disambiguates against existing filenames in the same folder by appending `" 2"`, `" 3"`, etc.
8. Scans the vault for `[[Untitled X]]` and `[[Untitled X|Display]]` references and rewrites them to point at the new filename (preserving display text).
9. Renames the file.

The filename itself is the idempotency marker — once a note is no longer named "Untitled", subsequent runs ignore it. No frontmatter timestamp needed.

## Repo layout

```
rename_notes.py        # main script (entry point for cron)
rename_notes.md        # prompt template
config.yaml.example    # config template (commit this)
config.yaml            # your local config (gitignored, copied from .example)
requirements.txt
.venv/                 # gitignored
logs/                  # gitignored, dated log per run
```

## Setup

Requires Python 3.13. The Ollama server must have `qwen2.5:7b` pulled (or whichever model you configure).

```bash
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml
```

Edit `config.yaml` to point `INPUT_FOLDER` at your vault, set `OLLAMA_SERVER_ADDRESS`, and adjust other settings.

## Daily run (cron)

```cron
30 0 * * * /path/to/obsidian-auto-name/.venv/bin/python /path/to/obsidian-auto-name/rename_notes.py >/dev/null 2>&1
```

Output is captured in `logs/rename_notes_YYYY-MM-DD.log`.

## Manual runs

```bash
# Dry run: log what would change without touching files
.venv/bin/python rename_notes.py --dry-run

# Process at most N notes (useful for first test)
.venv/bin/python rename_notes.py --limit 5

# Combine for safe exploration
.venv/bin/python rename_notes.py --dry-run --limit 5

# Verbose
.venv/bin/python rename_notes.py --debug
```

## Configuration reference

| Key                          | Default                  | Notes                                                       |
|------------------------------|--------------------------|-------------------------------------------------------------|
| `INPUT_FOLDER`               | `~/Documents/Notes`      | Vault root                                                  |
| `EXCLUDE_FOLDERS`            | `[]`                     | Subtrees to skip                                            |
| `LLM_PROVIDER`               | `ollama`                 | Only ollama supported                                       |
| `OLLAMA_MODEL`               | `qwen2.5:7b`             | Title-generation model                                      |
| `OLLAMA_SERVER_ADDRESS`      | `http://localhost:11434` | Ollama endpoint                                             |
| `OLLAMA_CONTEXT_WINDOW`      | `32000`                  | num_ctx                                                     |
| `RENAME_PATTERNS`            | `["Untitled","New Note"]`| Literal basenames that trigger renaming; ` N` suffix auto-handled |
| `MAX_TITLE_WORDS`            | `3`                      | Hard cap on words in the title; extra words trimmed         |
| `TITLE_CASE`                 | `title`                  | `title` \| `sentence` \| `lower` (preserves acronyms)       |
| `TITLE_TEMPLATE`             | `"{title}"`              | Filename template; `{title}` and `{date}` are substituted   |
| `CONFIDENCE_THRESHOLD`       | `0.5`                    | LLM confidence below this triggers first-line fallback      |
| `MAX_FILENAME_CHARS`         | `50`                     | Hard cap on final filename length (excluding `.md`)         |
| `OPT_OUT_FRONTMATTER_KEY`    | `"auto_name"`            | Frontmatter key — value `false` opts a note out entirely    |
| `DELETE_EMPTY_UNTITLED`      | `true`                   | Delete Untitled notes whose body is below the threshold     |
| `EMPTY_NOTE_BODY_MIN_CHARS`  | `1`                      | Threshold for "empty" — body chars after stripping FM       |
| `MAX_NOTE_AGE_DAYS`          | `0`                      | If > 0, only process notes modified in last N days          |

## Design notes

- **Schema-enforced output.** Same pattern as the sibling `obsidian-auto-tagger`: Ollama's `format` field with a JSON schema means the model can't return prose or skip the title field. Word-count enforcement is done in Python after parsing rather than in the schema (regex-based length constraints in JSON schemas are inconsistent across models).
- **Wikilink rewrite is per-file.** For each rename, the script greps the vault for `[[<old-basename>]]` and rewrites those references before renaming the file. In practice users rarely link to "Untitled N" by name, but the pass is cheap and prevents the dangling-link case.
- **Filename collisions resolved by suffix.** If the LLM picks "Meeting Notes" but that file already exists, the new name becomes "Meeting Notes 2.md", then "Meeting Notes 3.md", and so on.
- **macOS Local Network privacy is per-binary** (see the sibling tagger repo). New venvs that talk to Ollama on a non-localhost address must be created with the same `python@3.13` binary that has the Local Network grant, or LAN calls will silently fail with `EHOSTUNREACH`.
