#!/usr/bin/env python3
"""obsidian-auto-name: rename Obsidian "Untitled" notes using an LLM.

Each night the script scans the vault for files whose basename matches the
Obsidian default-name pattern (`Untitled.md`, `Untitled 1.md`, `Untitled 2.md`,
...). For each one it:

  1. Optionally deletes the note if its body is below the empty-content threshold.
  2. Sends the body to an Ollama LLM with a grammar-constrained JSON schema and
     gets back a 1..N word title.
  3. Sanitizes, casing-normalizes, and disambiguates the title against existing
     filenames.
  4. Updates `[[Untitled X]]` wikilink references across the vault.
  5. Renames the file.

The filename itself is the idempotency marker — once a file is no longer named
"Untitled", it won't be picked up by subsequent runs.
"""
import os
import re
import sys
import json
import logging
import argparse
import requests
import yaml
from datetime import datetime, timedelta
from pathlib import Path


logger = logging.getLogger(__name__)


# Built from config (`RENAME_PATTERNS`). Defaults cover Obsidian's two
# auto-generated note naming conventions: "Untitled N" and "New Note N".
DEFAULT_RENAME_PATTERNS = ["Untitled", "New Note"]


def build_rename_pattern_re(patterns):
    """Compile a regex matching any of `patterns` as the basename.

    Matching is case-insensitive (different Obsidian configs and plugins emit
    different casings for the default name). The suffix accepts BOTH duplicate
    styles Obsidian uses: ` N` (e.g. `Untitled 3`) and ` (N)` (e.g. `New note (2)`).
    """
    cleaned = [p for p in (patterns or []) if isinstance(p, str) and p.strip()]
    if not cleaned:
        return None
    alternation = '|'.join(re.escape(p) for p in cleaned)
    return re.compile(
        rf'^(?:{alternation})(?:\s+\d+|\s+\(\d+\))?\.md$',
        re.IGNORECASE,
    )

# JSON schema forcing the LLM to emit {"title": ..., "confidence": ...}.
# The confidence number drives a Notion-style fallback to the note's first line
# when the model judges the topic ambiguous.
TITLE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["title", "confidence"],
}

# H1 specifically, anchored to a single '#' (not '##' or deeper).
H1_LINE_RE = re.compile(r'^#\s+(.+?)\s*$')

# Strict: any non-alphanumeric, non-space character gets stripped. This catches
# filesystem-unsafe chars AND stylistic noise like `+`, `&`, `@`, `#` that the
# LLM might slip in. The prompt asks for words-only; this is the safety net.
FILENAME_UNSAFE_RE = re.compile(r'[^A-Za-z0-9 ]')

# Match [[Untitled 3]] and [[Untitled 3|Display Text]] in note bodies.
def wikilink_re_for(target_basename):
    """Build a regex that matches wikilinks to a specific basename, case-insensitively
    (Obsidian resolves wikilinks case-insensitively on most filesystems, so a user
    might have written `[[new note]]` to link to `New Note.md`). Captures group 1
    = optional display text after `|`, or empty string."""
    escaped = re.escape(target_basename)
    return re.compile(r'\[\[' + escaped + r'(\|[^\]]+)?\]\]', re.IGNORECASE)


# ---------- Config ----------

def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    defaults = {
        "INPUT_FOLDER": os.path.expanduser("~/Documents/Notes"),
        "EXCLUDE_FOLDERS": [],
        "LLM_PROVIDER": "ollama",
        "OLLAMA_MODEL": "qwen2.5:7b",
        "OLLAMA_SERVER_ADDRESS": "http://localhost:11434",
        "OLLAMA_CONTEXT_WINDOW": 32000,
        "RENAME_PATTERNS": list(DEFAULT_RENAME_PATTERNS),
        "MAX_TITLE_WORDS": 3,
        "TITLE_CASE": "title",
        "TITLE_TEMPLATE": "{title}",
        "CONFIDENCE_THRESHOLD": 0.5,
        "MAX_FILENAME_CHARS": 50,
        "OPT_OUT_FRONTMATTER_KEY": "auto_name",
        "DELETE_EMPTY_UNTITLED": True,
        "EMPTY_NOTE_BODY_MIN_CHARS": 1,
        "MAX_NOTE_AGE_DAYS": 0,
    }
    config_path = os.path.join(script_dir, "config.yaml")
    if not os.path.exists(config_path):
        logger.warning(f"No config.yaml at {config_path}; using defaults")
        return defaults
    with open(config_path, 'r') as f:
        user = yaml.safe_load(f) or {}
    merged = {**defaults, **user}
    logger.info(f"Loaded config from {config_path}")
    return merged


# ---------- Note scanning ----------

def parse_frontmatter(content):
    """Return (frontmatter_dict, body_string). Empty dict if no frontmatter."""
    m = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    if not m:
        return {}, content
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, content[m.end():]


def find_untitled_notes(input_folder, exclude_folders, pattern_re, max_age_days=0):
    """Walk the vault and return absolute paths of files matching pattern_re.
    When max_age_days > 0, only include files whose mtime is within that window."""
    results = []
    cutoff = None
    if max_age_days and max_age_days > 0:
        cutoff = datetime.now() - timedelta(days=max_age_days)

    for root, _, files in os.walk(input_folder):
        if any(root.startswith(e) for e in exclude_folders):
            continue
        for fname in files:
            if not pattern_re.match(fname):
                continue
            path = os.path.join(root, fname)
            if cutoff is not None:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
                if mtime < cutoff:
                    continue
            results.append(path)
    return sorted(results)


def note_body_length(content):
    """Length of body content after stripping frontmatter and whitespace."""
    _, body = parse_frontmatter(content)
    return len(body.strip())


# ---------- Title sources ----------

def extract_frontmatter_title(frontmatter, pattern_re):
    """Return a usable title from the YAML frontmatter `title:` field, or ''.

    Skipped if the frontmatter title itself matches one of the rename patterns
    (e.g. `title: Untitled 3`) — avoids the edge case where a templating plugin
    auto-fills the same name we're trying to escape from.
    """
    if not isinstance(frontmatter, dict):
        return ""
    title = frontmatter.get("title")
    if not isinstance(title, str):
        return ""
    title = title.strip()
    if not title:
        return ""
    if pattern_re.match(title + ".md"):
        return ""
    return title


def extract_h1_title(body):
    """Return the H1 text iff the first non-blank line of the body is an H1.

    We deliberately don't match H1s deeper in the note (those are subsection
    headers, not titles) or H2/H3 anywhere. Conservative — only fires when the
    user clearly intended the heading to act as a title.
    """
    if not body:
        return ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = H1_LINE_RE.match(stripped)
        if m:
            return m.group(1).strip()
        return ""  # first non-blank line isn't H1; don't keep searching
    return ""


def extract_first_nonempty_line(body):
    """Notion-style fallback: the first non-blank line of the body, with any
    leading markdown markers (`#`, `-`, `*`, `>`) stripped. Used when the LLM
    is below the confidence threshold."""
    if not body:
        return ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading markdown markers
        line = re.sub(r'^[#>\-\*\+\s]+', '', line).strip()
        if line:
            return line
    return ""


def call_ollama_for_title(body, prompt_template, config):
    """Send the note body to Ollama with grammar-constrained JSON output.
    Returns (title, confidence) — ('', 0.0) on any failure."""
    model = config["OLLAMA_MODEL"]
    server = config["OLLAMA_SERVER_ADDRESS"]
    max_words = int(config.get("MAX_TITLE_WORDS", 3))

    full_prompt = prompt_template.replace("{MAX_WORDS}", str(max_words)) + "\n\n" + body

    payload = {
        "model": model,
        "prompt": full_prompt,
        "stream": False,
        "format": TITLE_OUTPUT_SCHEMA,
        "options": {
            "num_ctx": int(config.get("OLLAMA_CONTEXT_WINDOW", 32000)),
            "temperature": 0.1,
        },
    }
    try:
        r = requests.post(
            f"{server}/api/generate",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=300,
        )
    except Exception as e:
        logger.error(f"Ollama request failed: {e}")
        return "", 0.0

    if r.status_code != 200:
        logger.error(f"Ollama API error: {r.status_code} {r.text[:200]}")
        return "", 0.0

    raw = r.json().get("response", "").strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Title JSON parse failed: {e}; raw={raw[:200]!r}")
        return "", 0.0

    if not isinstance(parsed, dict):
        logger.error(f"Title response not an object: {parsed!r}")
        return "", 0.0

    title = parsed.get("title", "")
    if not isinstance(title, str):
        title = ""
    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    return title.strip(), confidence


# ---------- Title normalization ----------

def sanitize_filename(title):
    """Strip filesystem-unsafe characters and trim whitespace/periods.
    Replace any unsafe char with a space, then collapse runs of whitespace."""
    cleaned = FILENAME_UNSAFE_RE.sub(' ', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    return cleaned


def _smart_titleize(title):
    """Title-case the input but preserve acronyms and existing mixed-case tokens.

    Python's str.title() naively lowercases everything after the first letter,
    which mangles legit acronyms ('RFQ' -> 'Rfq') and camelCase ('SimpleFin' ->
    'Simplefin'). We instead leave any token alone that's already all-upper or
    that has uppercase letters past position 0; everything else gets capitalize.
    """
    out = []
    for tok in title.split():
        if not tok:
            continue
        if tok.isupper():               # already all-caps acronym (RFQ, JSON)
            out.append(tok)
        elif any(c.isupper() for c in tok[1:]):  # mixed-case (SimpleFin, iOS)
            out.append(tok)
        elif tok[0].isalpha():
            out.append(tok[0].upper() + tok[1:].lower())
        else:
            out.append(tok)              # non-alpha leading char (numbers, +)
    return ' '.join(out)


def _smart_sentence_case(title):
    """Like sentence case but preserve acronyms and mixed-case tokens (capitalize
    only the first word, leave the rest alone unless they were lowercase)."""
    out = []
    for i, tok in enumerate(title.split()):
        if not tok:
            continue
        if tok.isupper():
            out.append(tok)
        elif any(c.isupper() for c in tok[1:]):
            out.append(tok)
        elif i == 0 and tok[0].isalpha():
            out.append(tok[0].upper() + tok[1:].lower())
        else:
            out.append(tok.lower())
    return ' '.join(out)


def apply_casing(title, mode):
    if mode == "title":
        return _smart_titleize(title)
    if mode == "sentence":
        return _smart_sentence_case(title)
    if mode == "lower":
        return title.lower()
    return title


# Stop words that look awkward as the last word of a title, especially after
# we've truncated to the word budget ('Bank Sync And' should become 'Bank Sync').
TRAILING_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "for",
    "in", "on", "at", "by", "with", "vs", "from",
}


def trim_to_max_words(title, max_words):
    """Trim to max_words, then strip any trailing stop words to avoid awkward
    sentence fragments ('Bank Sync And' -> 'Bank Sync')."""
    words = title.split()
    if len(words) > max_words:
        words = words[:max_words]
    while words and words[-1].lower() in TRAILING_STOP_WORDS:
        words.pop()
    return ' '.join(words)


def normalize_title(raw_title, config):
    """Run a candidate title through sanitize -> trim -> casing. Empty string on
    total failure (caller skips the file). Used uniformly for LLM output, H1
    headings, and first-line fallback so all sources produce consistent results.
    """
    if not raw_title:
        return ""
    title = sanitize_filename(raw_title)
    if not title:
        return ""
    title = trim_to_max_words(title, int(config.get("MAX_TITLE_WORDS", 3)))
    title = apply_casing(title, config.get("TITLE_CASE", "title"))
    return title


def apply_title_template(title, file_path, template):
    """Substitute {title} and {date} (file mtime, YYYY-MM-DD) in the template.
    If the template doesn't reference {title}, we still inject the title at the
    end as a safety net."""
    if not template:
        return title
    mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
    date_str = mtime.strftime("%Y-%m-%d")
    result = template.replace("{title}", title).replace("{date}", date_str)
    result = re.sub(r'\s+', ' ', result).strip()
    if "{title}" not in template and title not in result:
        result = f"{result} {title}".strip()
    return result


# ---------- Filename collision ----------

def unique_filename(directory, base_title):
    """Append ' 2', ' 3', ... until we find a non-colliding filename."""
    candidate = base_title + ".md"
    if not os.path.exists(os.path.join(directory, candidate)):
        return candidate
    i = 2
    while True:
        candidate = f"{base_title} {i}.md"
        if not os.path.exists(os.path.join(directory, candidate)):
            return candidate
        i += 1


# ---------- Wikilink rewrite ----------

def rewrite_wikilinks(input_folder, exclude_folders, old_basename, new_basename, dry_run=False):
    """Rewrite [[old_basename]] and [[old_basename|display]] across the vault.
    Returns the count of files updated."""
    pattern = wikilink_re_for(old_basename)
    files_changed = 0

    for root, _, files in os.walk(input_folder):
        if any(root.startswith(e) for e in exclude_folders):
            continue
        for fname in files:
            if not fname.endswith('.md'):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            if old_basename not in content:
                continue

            def repl(m):
                display = m.group(1) or ''  # includes leading '|' if present
                return f'[[{new_basename}{display}]]'

            new_content, n = pattern.subn(repl, content)
            if n == 0:
                continue
            files_changed += 1
            if dry_run:
                logger.info(f"  [dry-run] would rewrite {n} wikilink(s) in {os.path.relpath(path, input_folder)}")
                continue
            tmp = path + '.rename.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp, path)
            logger.info(f"  rewrote {n} wikilink(s) in {os.path.relpath(path, input_folder)}")

    return files_changed


# ---------- Main per-file work ----------

def process_untitled(path, prompt_template, config, dry_run, pattern_re):
    """Process a single Untitled note. Returns one of: 'renamed', 'deleted',
    'skipped', 'failed'."""
    directory = os.path.dirname(path)
    basename = os.path.basename(path)
    stem = basename[:-3]  # strip .md
    rel = os.path.relpath(path, config["INPUT_FOLDER"])

    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"  read failed for {rel}: {e}")
        return 'failed'

    frontmatter, body = parse_frontmatter(content)

    # Honor a frontmatter opt-out flag (default key: `auto_name`). A value of
    # `false` means "leave this note alone forever".
    opt_out_key = config.get("OPT_OUT_FRONTMATTER_KEY", "auto_name")
    if opt_out_key and frontmatter.get(opt_out_key) is False:
        logger.info(f"  skip: opted out via frontmatter `{opt_out_key}: false`")
        return 'skipped'

    body_len = len(body.strip())
    if body_len < int(config.get("EMPTY_NOTE_BODY_MIN_CHARS", 1)):
        if config.get("DELETE_EMPTY_UNTITLED", True):
            if dry_run:
                logger.info(f"  [dry-run] would delete empty note ({body_len} body chars)")
            else:
                os.remove(path)
                logger.info(f"  deleted empty note ({body_len} body chars)")
            return 'deleted'
        else:
            logger.info(f"  skip: body too short ({body_len} chars) and DELETE_EMPTY_UNTITLED is false")
            return 'skipped'

    # Source priority: frontmatter title > H1 heading > LLM (if confident) > first-line fallback.
    raw_title = ""
    source = ""

    fm_title = extract_frontmatter_title(frontmatter, pattern_re)
    if fm_title:
        raw_title = fm_title
        source = "frontmatter"
    else:
        heading = extract_h1_title(body)
        if heading:
            raw_title = heading
            source = "h1"
        else:
            llm_title, confidence = call_ollama_for_title(body, prompt_template, config)
            threshold = float(config.get("CONFIDENCE_THRESHOLD", 0.5))
            if llm_title and confidence >= threshold:
                raw_title = llm_title
                source = f"llm(conf={confidence:.2f})"
            else:
                firstline = extract_first_nonempty_line(body)
                if firstline:
                    raw_title = firstline
                    source = f"firstline(llm_conf={confidence:.2f})"
                else:
                    logger.warning(f"  no usable title source for {rel} (llm={llm_title!r} conf={confidence:.2f})")
                    return 'failed'

    title = normalize_title(raw_title, config)
    if not title:
        logger.warning(f"  normalized title empty for {rel} (raw={raw_title!r}, source={source})")
        return 'failed'

    template = config.get("TITLE_TEMPLATE", "{title}")
    final_title = apply_title_template(title, path, template)
    final_title = sanitize_filename(final_title)  # safety net if template introduced bad chars
    if not final_title:
        logger.warning(f"  final title empty after template for {rel} (title={title!r})")
        return 'failed'

    # Hard cap on filename length, truncating at the last word boundary to avoid
    # ending in a partial word.
    max_chars = int(config.get("MAX_FILENAME_CHARS", 50))
    if max_chars > 0 and len(final_title) > max_chars:
        truncated = final_title[:max_chars].rstrip()
        if ' ' in truncated:
            truncated = truncated.rsplit(' ', 1)[0]
        logger.info(f"  truncated title to {len(truncated)} chars ({final_title!r} -> {truncated!r})")
        final_title = truncated

    new_filename = unique_filename(directory, final_title)
    new_stem = new_filename[:-3]
    new_path = os.path.join(directory, new_filename)

    if dry_run:
        logger.info(f"  [dry-run] '{stem}' -> '{new_stem}'  (source: {source}, raw: {raw_title!r})")
        return 'renamed'

    logger.info(f"  '{stem}' -> '{new_stem}'  (source: {source}, raw: {raw_title!r})")
    rewrite_wikilinks(config["INPUT_FOLDER"], config["EXCLUDE_FOLDERS"], stem, new_stem, dry_run=False)
    os.rename(path, new_path)
    return 'renamed'


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(description="Rename Obsidian Untitled notes using an LLM-generated title")
    ap.add_argument('--dry-run', action='store_true',
                    help='Log proposed renames/deletions without modifying any files')
    ap.add_argument('--limit', type=int, default=0,
                    help='Process at most N notes (0 = no limit)')
    ap.add_argument('--debug', action='store_true', help='Verbose logging')
    args = ap.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info("=== Starting rename_notes.py ===")
    if args.dry_run:
        logger.info("DRY RUN mode — no files will be modified")

    config = load_config()
    logger.info(f"Vault: {config['INPUT_FOLDER']}")
    logger.info(f"Max title words: {config['MAX_TITLE_WORDS']}; casing: {config['TITLE_CASE']}")
    logger.info(f"Delete empty: {config['DELETE_EMPTY_UNTITLED']} (threshold: {config['EMPTY_NOTE_BODY_MIN_CHARS']} body chars)")
    if config.get("MAX_NOTE_AGE_DAYS", 0):
        logger.info(f"Age filter: only notes modified in last {config['MAX_NOTE_AGE_DAYS']} days")

    # Load prompt
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, "rename_notes.md")
    if not os.path.exists(prompt_path):
        logger.error(f"Prompt template not found at {prompt_path}")
        return 1
    with open(prompt_path, 'r') as f:
        prompt_template = f.read()

    # Compile the rename-pattern regex from config
    rename_patterns = config.get("RENAME_PATTERNS") or DEFAULT_RENAME_PATTERNS
    pattern_re = build_rename_pattern_re(rename_patterns)
    if pattern_re is None:
        logger.error("RENAME_PATTERNS is empty; nothing to match")
        return 1
    logger.info(f"Matching note names: {rename_patterns}")

    # Find Untitled notes
    candidates = find_untitled_notes(
        config["INPUT_FOLDER"],
        config["EXCLUDE_FOLDERS"],
        pattern_re,
        max_age_days=int(config.get("MAX_NOTE_AGE_DAYS", 0)),
    )
    logger.info(f"Found {len(candidates)} matching note(s)")
    if not candidates:
        logger.info("Nothing to do.")
        return 0

    if args.limit > 0 and len(candidates) > args.limit:
        logger.info(f"Limiting to {args.limit} (out of {len(candidates)})")
        candidates = candidates[:args.limit]

    counts = {'renamed': 0, 'deleted': 0, 'skipped': 0, 'failed': 0}
    for i, path in enumerate(candidates, 1):
        rel = os.path.relpath(path, config["INPUT_FOLDER"])
        logger.info(f"[{i}/{len(candidates)}] {rel}")
        result = process_untitled(path, prompt_template, config, args.dry_run, pattern_re)
        counts[result] += 1

    logger.info(f"=== Summary: renamed={counts['renamed']} deleted={counts['deleted']} skipped={counts['skipped']} failed={counts['failed']} ===")
    return 0


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"rename_notes_{datetime.now().strftime('%Y-%m-%d')}.log")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger(__name__)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Unhandled exception: {e}")
        sys.exit(1)
