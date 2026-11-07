#!/usr/bin/env python3
"""Generate the Garage stats page into a vault checkout.

Writes `Garage stats.md` and `public-vault-stats.json` into --vault-path.
Run it only against a clone or the CI checkout, never the live Obsidian vault.

File stats come from git metadata (file names, commit dates). Line stats replay
the diffs of Markdown notes, so each surviving line keeps the quarter it was
written in. The public-note count reads current frontmatter. Paths with any
segment that starts with a dot (.obsidian, .github, .gitignore, ...) are left out.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

import frontmatter

TZ = ZoneInfo("America/Chicago")
PAGE_NAME = "Garage stats.md"
DATA_NAME = "public-vault-stats.json"
HEADER = re.compile(r"^@([0-9a-f]{40}) (\S+)$")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LOG_ARGS = ["log", "--reverse", "--first-parent", "--diff-merges=first-parent", "-M"]


def is_hidden(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/"))


def is_counted(path: str) -> bool:
    """Counted paths: not in a hidden folder, and not this script's own output."""
    return not is_hidden(path) and path not in (PAGE_NAME, DATA_NAME)


def is_counted_note(path: str) -> bool:
    return path.endswith(".md") and is_counted(path)


def quarter_of(moment: datetime) -> str:
    local = moment.astimezone(TZ)
    return f"{local.year} Q{(local.month - 1) // 3 + 1}"


def git(vault: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=vault,
        input=b"",  # never wait on the terminal; hash-object --stdin reads this
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", "surrogateescape")


# ---------- weekly sampling, shared by the file and line replays ----------


def next_monday(moment: datetime) -> datetime:
    local = moment.astimezone(TZ)
    day = local.date() + timedelta(days=7 - local.weekday())
    return datetime(day.year, day.month, day.day, tzinfo=TZ)


def weekly_replay(events: Iterable[tuple[datetime, object]], state, now: datetime):
    """Apply (time, payload) events to `state`, sampling its cohort counts every Sunday.

    Returns the weekly samples and (time, total) after each event.
    """
    samples: list[tuple[date, dict[str, int]]] = []
    timeline: list[tuple[datetime, int]] = []
    boundary = None
    for time, payload in events:
        if boundary is None:
            boundary = next_monday(time)
        while time >= boundary:
            samples.append(((boundary - timedelta(days=1)).date(), state.snapshot()))
            boundary += timedelta(days=7)
        state.apply(time, payload)
        timeline.append((time, state.total))
    while boundary is not None and boundary <= now:
        samples.append(((boundary - timedelta(days=1)).date(), state.snapshot()))
        boundary += timedelta(days=7)
    samples.append((now.astimezone(TZ).date(), state.snapshot()))
    return samples, timeline


def snapshot_of(counts: Counter[str]) -> dict[str, int]:
    return {q: n for q, n in sorted(counts.items()) if n > 0}


# ---------- files: one pass over --name-status ----------


def parse_name_status(raw: str) -> list[tuple[datetime, list[tuple[str, str, str | None]]]]:
    """Parse `git log --name-status -z --format='@%H %cI'` output."""
    commits: list[tuple[datetime, list]] = []
    tokens = iter(raw.split("\0"))
    for token in tokens:
        token = token.lstrip("\n")
        if not token:
            continue
        match = HEADER.match(token)
        if match:
            commits.append((datetime.fromisoformat(match.group(2)), []))
            continue
        status = token  # e.g. "A", "M", "D", or "R087" (rename with similarity score)
        if status[0] in "RC":
            old, new = next(tokens), next(tokens)
            commits[-1][1].append((status, old, new))
        else:
            commits[-1][1].append((status, next(tokens), None))
    return commits


class FileCohorts:
    """Current counted paths, each tagged with the quarter it was added and its last edit."""

    def __init__(self) -> None:
        self.paths: dict[str, str] = {}
        self.edited: dict[str, datetime] = {}
        self.counts: Counter[str] = Counter()
        self.new_by_day: Counter[date] = Counter()  # new files per day (renames aren't new)

    def add_new(self, path: str, quarter: str, time: datetime) -> None:
        if is_counted(path) and path not in self.paths:
            self.new_by_day[time.astimezone(TZ).date()] += 1
        self.add(path, quarter, time)

    def add(self, path: str, quarter: str, edited: datetime) -> None:
        if not is_counted(path):
            return
        self.remove(path)
        self.paths[path] = quarter
        self.edited[path] = edited
        self.counts[quarter] += 1

    def remove(self, path: str) -> tuple[str, datetime] | None:
        quarter = self.paths.pop(path, None)
        if quarter is None:
            return None
        self.counts[quarter] -= 1
        return quarter, self.edited.pop(path)

    def apply(self, time: datetime, changes) -> None:
        quarter = quarter_of(time)
        for status, path, new_path in changes:
            kind = status[0]
            if kind == "D":
                self.remove(path)
            elif kind == "R":
                original = self.remove(path)
                if original is None:  # renamed in from an uncounted path (e.g. a hidden folder)
                    self.add_new(new_path, quarter, time)
                    continue
                # a pure rename (score 100) isn't an edit, so it keeps the earlier date
                self.add(new_path, original[0], original[1] if status[1:] == "100" else time)
            elif kind == "C":
                self.add_new(new_path, quarter, time)
            elif path in self.paths and kind != "A":
                self.edited[path] = time  # M or T: an edit
            else:
                self.add_new(path, quarter, time)

    @property
    def total(self) -> int:
        return len(self.paths)

    def snapshot(self) -> dict[str, int]:
        return snapshot_of(self.counts)


# ---------- lines: one pass over the Markdown patches ----------

ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of unusual file names."""
    if not (len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')):
        return raw
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
            else:
                out.append(ESCAPES.get(nxt, ord(nxt)))
                i += 2
        else:
            out += body[i].encode("utf-8", "surrogateescape")
            i += 1
    return out.decode("utf-8", "surrogateescape")


def strip_side(path: str, side: str) -> str:
    return path[2:] if path.startswith(side + "/") else path


def header_paths(rest: str) -> tuple[str, str]:
    """Paths from `diff --git a/X b/Y`. Renames are corrected later by `rename from/to`."""
    if rest.startswith('"'):
        end = 1
        while end < len(rest) and not (rest[end] == '"' and rest[end - 1] != "\\"):
            end += 1
        old, new = unquote_path(rest[: end + 1]), unquote_path(rest[end + 2 :])
    else:
        half = (len(rest) - 1) // 2  # unquoted names are only ambiguous for renames
        old, new = rest[:half], rest[half + 1 :]
    return strip_side(old, "a"), strip_side(new, "b")


@dataclass
class FileDiff:
    old: str | None
    new: str | None
    hunks: list[tuple[int, int, int]] = field(default_factory=list)  # (old start, old len, new len)
    binary: bool = False


def parse_patches(lines: Iterable[str]) -> Iterator[tuple[datetime, list[FileDiff]]]:
    """Parse `git log -p --unified=0 --format='@%H %cI'` output, one commit at a time."""
    time: datetime | None = None
    diffs: list[FileDiff] = []
    current: FileDiff | None = None
    pending = 0  # hunk body lines still to skip
    for line in lines:
        line = line.rstrip("\n")
        if pending:
            if not line.startswith("\\"):
                pending -= 1
            continue
        if line.startswith("@"):
            match = HEADER.match(line)
            if match:
                if time is not None:
                    yield time, diffs
                time, diffs, current = datetime.fromisoformat(match.group(2)), [], None
                continue
            hunk = HUNK.match(line)
            if hunk and current is not None:
                old_len = 1 if hunk.group(2) is None else int(hunk.group(2))
                new_len = 1 if hunk.group(4) is None else int(hunk.group(4))
                current.hunks.append((int(hunk.group(1)), old_len, new_len))
                pending = old_len + new_len
            continue
        if line.startswith("diff --git "):
            old, new = header_paths(line[len("diff --git ") :])
            current = FileDiff(old, new)
            diffs.append(current)
        elif current is None:
            continue
        elif line.startswith("rename from "):
            current.old = unquote_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            current.new = unquote_path(line[len("rename to ") :])
        elif line.startswith("new file mode"):
            current.old = None
        elif line.startswith("deleted file mode"):
            current.new = None
        elif line.startswith("Binary files "):
            current.binary = True
    if time is not None:
        yield time, diffs


class LineCohorts:
    """Lines of every note, each tagged with the quarter it was written in."""

    def __init__(self) -> None:
        self.files: dict[str, list[str]] = {}
        self.counts: Counter[str] = Counter()
        # lines written per day: new and edited lines in counted notes (deletions not subtracted)
        self.written_by_day: Counter[date] = Counter()

    def _count(self, path: str, lines: list[str], sign: int) -> None:
        if is_counted_note(path):
            for quarter, n in Counter(lines).items():
                self.counts[quarter] += sign * n

    def apply(self, time: datetime, diffs: list[FileDiff]) -> None:
        quarter = quarter_of(time)
        for diff in diffs:
            old_lines = self.files.pop(diff.old, []) if diff.old else []
            if diff.old:
                self._count(diff.old, old_lines, -1)
            if diff.new is None:
                continue
            if diff.binary:
                new_lines: list[str] = []
            else:
                new_lines, index = [], 0
                for start, old_len, new_len in diff.hunks:
                    # an insertion (old_len 0) goes after line `start`
                    begin = start - 1 if old_len else start
                    new_lines.extend(old_lines[index:begin])
                    new_lines.extend([quarter] * new_len)
                    index = begin + old_len
                new_lines.extend(old_lines[index:])
                if is_counted_note(diff.new):
                    self.written_by_day[time.astimezone(TZ).date()] += sum(n for _, _, n in diff.hunks)
            self.files[diff.new] = new_lines
            self._count(diff.new, new_lines, +1)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> dict[str, int]:
        return snapshot_of(self.counts)


def replay_lines(vault: Path, now: datetime):
    state = LineCohorts()
    with subprocess.Popen(
        ["git", "-c", "core.quotePath=false", *LOG_ARGS, "-p", "--unified=0", "--no-color",
         "--no-ext-diff", "--format=@%H %cI", "--", "*.md"],
        cwd=vault,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    ) as process:
        samples, _ = weekly_replay(parse_patches(process.stdout), state, now)
    if process.returncode != 0:
        raise RuntimeError("git log -p failed")
    return state, samples


def head_line_counts(vault: Path) -> dict[str, int]:
    """Line count of every counted note at HEAD, as git counts them."""
    empty_tree = git(vault, "hash-object", "-t", "tree", "--stdin").strip()
    tokens = git(vault, "diff", "--numstat", "-z", empty_tree, "HEAD", "--", "*.md").split("\0")
    counts = {}
    for token in tokens:
        if token:
            added, _deleted, path = token.split("\t", 2)
            if is_counted_note(path):
                counts[path] = 0 if added == "-" else int(added)
    return counts


# ---------- note metadata: syndication and tags ----------

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)^---\s*$", re.DOTALL | re.MULTILINE)
SYNDICATION_KEY = re.compile(r"^(publish|syndicate)\s*:", re.MULTILINE)
TAGS_KEY = re.compile(r"^tags\s*:[ \t]*(.*)$", re.MULTILINE)
LIST_ITEM = re.compile(r"^[ \t]*-[ \t]*(.*?)[ \t]*$")
TAG_FAMILIES = ("type", "topic")


@dataclass
class NoteMeta:
    public: bool = False  # publish: true
    ai: bool = False  # syndicate: ai
    tags: set[str] = field(default_factory=set)  # first-level type/... and topic/... tags


def first_level(tag: object) -> str | None:
    """`type/reference/website` → `type/reference`; `task`, `task/x` → `type/task`; else None."""
    name = str(tag).strip().strip("\"'").lstrip("#").strip().lower()
    parts = name.split("/")
    if parts[0] == "task":
        return "type/task"
    if parts[0] in TAG_FAMILIES and len(parts) > 1 and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


def tags_from_block(block: str) -> list[str]:
    """Raw `tags` values from a frontmatter block without a YAML parse.

    Handles a list (`tags:` then `- a` lines), `tags: [a, b]`, and `tags: a`.
    """
    match = TAGS_KEY.search(block)
    if match is None:
        return []
    inline = match.group(1).strip()
    if inline:
        return [t for t in inline.strip("[]").split(",") if t.strip()]
    values = []
    for line in block[match.end() :].lstrip("\r\n").splitlines():
        item = LIST_ITEM.match(line)
        if item is None:
            break  # the next key (or anything else) ends the list
        values.append(item.group(1))
    return values


def note_metadata(path: Path) -> NoteMeta:
    """Syndication flags and first-level tags for one note.

    Full YAML parsing is slow, so it only runs for notes whose frontmatter has a `publish`
    or `syndicate` key. Every published note is parsed that way, so the list of tags that
    are safe to name always comes from properly parsed YAML. Other notes' tags come from
    a light pattern match.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return NoteMeta()
    block = FRONTMATTER.match(text)
    if block is None:
        return NoteMeta()
    if not SYNDICATION_KEY.search(block.group(1)):
        raw_tags = tags_from_block(block.group(1))
        return NoteMeta(tags={t for t in map(first_level, raw_tags) if t})
    try:
        metadata = frontmatter.loads(text).metadata
    except Exception:
        return NoteMeta()
    publish, syndicate, raw = metadata.get("publish"), metadata.get("syndicate"), metadata.get("tags")
    raw_tags = raw if isinstance(raw, list) else ([] if raw is None else str(raw).split(","))
    return NoteMeta(
        public=publish is True or publish == "true",
        ai=syndicate == "ai" or (isinstance(syndicate, list) and "ai" in syndicate),
        tags={t for t in map(first_level, raw_tags) if t},
    )


def tag_counts(metas: list[NoteMeta]) -> dict[str, list[dict]]:
    """Notes per first-level tag, for each family, naming only tags on published notes.

    A note counts once under each named tag it has. Tags that no published note carries
    are never named: a note with any of them counts once under "other". Notes with no tag
    in the family count under "none". Named tags are sorted by count; other and none last.
    """
    allowed = {t for m in metas if m.public for t in m.tags}
    result: dict[str, list[dict]] = {}
    for family in TAG_FAMILIES:
        named: Counter[str] = Counter()
        other = none = 0
        for meta in metas:
            mine = {t for t in meta.tags if t.startswith(family + "/")}
            if not mine:
                none += 1
                continue
            named.update(mine & allowed)
            other += bool(mine - allowed)
        rows = [{"name": name, "notes": n} for name, n in sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))]
        rows += [{"name": "other", "notes": other}, {"name": "none", "notes": none}]
        result[family] = rows
    return result


# ---------- stats ----------


AVERAGE_WINDOWS = (7, 30)  # trailing averages, in days


def daily_series(counts: Counter[date], first: date, until: datetime) -> list[dict]:
    """A count per day (Chicago time) from `first` through today, which is still in
    progress, with trailing 7- and 30-day averages once each has a full window.

    Each day has "count", "average_7" and "average_30" (None until the window is full).
    """
    day, last = first, until.astimezone(TZ).date()
    days: list[dict] = []
    while day <= last:
        entry = {"date": day.isoformat(), "count": counts.get(day, 0)}
        days.append(entry)
        for window in AVERAGE_WINDOWS:
            recent = days[-window:]
            entry[f"average_{window}"] = (
                round(sum(d["count"] for d in recent) / window, 1) if len(recent) == window else None
            )
        day += timedelta(days=1)
    return days


def last_edited_months(edited: dict[str, datetime], first: datetime, until: datetime) -> list[dict]:
    """Files per calendar month of their last edit, every month from the first commit on."""
    counts = Counter(f"{t.astimezone(TZ):%Y-%m}" for t in edited.values())
    month = first.astimezone(TZ).date().replace(day=1)
    last = until.astimezone(TZ).date().replace(day=1)
    months = []
    while month <= last:
        months.append({"month": f"{month:%Y-%m}", "files": counts.get(f"{month:%Y-%m}", 0)})
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return months


def collect_stats(vault: Path, now: datetime) -> dict:
    commits = parse_name_status(git(vault, *LOG_ARGS, "--name-status", "-z", "--format=@%H %cI"))
    files = FileCohorts()
    file_samples, timeline = weekly_replay(commits, files, now)

    current = {p for p in git(vault, "ls-files", "-z").split("\0") if p and is_counted(p)}
    if current != set(files.paths):
        missing, extra = sorted(current - set(files.paths)), sorted(set(files.paths) - current)
        raise RuntimeError(
            f"File replay does not match HEAD: {len(missing)} missing, {len(extra)} extra. "
            f"First missing: {missing[:3]} First extra: {extra[:3]}"
        )

    lines, line_samples = replay_lines(vault, now)
    replayed_lines = {p: len(v) for p, v in lines.files.items() if is_counted_note(p)}
    expected_lines = head_line_counts(vault)
    if replayed_lines != expected_lines:
        wrong = sorted(
            p for p in set(replayed_lines) | set(expected_lines)
            if replayed_lines.get(p) != expected_lines.get(p)
        )
        raise RuntimeError(f"Line replay does not match HEAD for {len(wrong)} notes, e.g. {wrong[:3]}")

    notes = sorted(p for p in current if p.endswith(".md"))
    metas = [note_metadata(vault / p) for p in notes]
    first_time, first_total = timeline[0]
    days = max((now - first_time).total_seconds() / 86400, 1)
    # every commit, merges included, so the days add up to the Commits stat
    commit_days = Counter(
        datetime.fromisoformat(t).astimezone(TZ).date() for t in git(vault, "log", "--format=%cI").split()
    )
    first_day = min(commit_days)
    return {
        "last_edited_months": last_edited_months(files.edited, first_time, now),
        "daily_commits": daily_series(commit_days, first_day, now),
        "daily_new_files": daily_series(files.new_by_day, first_day, now),
        "daily_new_lines": daily_series(lines.written_by_day, first_day, now),
        "generated": now.astimezone(TZ).isoformat(timespec="seconds"),
        "files": len(current),
        "lines": lines.total,
        "commits": int(git(vault, "rev-list", "--count", "HEAD").strip()),
        "files_per_day": round((len(current) - first_total) / days, 1),
        "first_commit": first_time.astimezone(TZ).date().isoformat(),
        "first_commit_files": first_total,
        "notes": len(notes),
        "public_notes": sum(1 for m in metas if m.public),
        "ai_notes": sum(1 for m in metas if m.ai),
        # only tags already on published notes are named; never store raw private tags here
        "tags": tag_counts(metas),
        "quarters": sorted({q for _, snap in file_samples + line_samples for q in snap}),
        "weekly_files": [{"date": d.isoformat(), "counts": s} for d, s in file_samples],
        "weekly_lines": [{"date": d.isoformat(), "counts": s} for d, s in line_samples],
    }


# ---------- rendering ----------

W, H = 720, 320
LEFT, RIGHT, TOP, BOTTOM = 52, 64, 10, 28  # the right margin holds the band labels
LABEL_GAP = 13  # minimum vertical spacing between band labels, in SVG pixels


def nice_step(maximum: int) -> int:
    for base in (1, 10, 100, 1000, 10000, 100000, 1000000):
        for mult in (1, 2, 5):
            if maximum / (base * mult) <= 6:
                return base * mult
    return 10000000


def tick_label(value: int) -> str:
    return f"{value / 1000:g}k" if value >= 10000 else f"{value:,}"


def quarter_label(quarter: str, first_quarter: str) -> str:
    return f"{quarter} (initial import)" if quarter == first_quarter else quarter


def chart_style() -> str:
    # Site theme variables only, so light and dark mode follow Quartz's theme toggle.
    # No quotes or angle brackets in this CSS: Quartz HTML-escapes them inside <style>
    return (
        "<style>"
        ".vs-chart{margin-bottom:1rem}"
        ".vs-chart svg{width:100%;height:auto;display:block;overflow:visible}"
        ".vs-chart .vs-grid{stroke:var(--lightgray);stroke-width:1}"
        ".vs-chart .vs-tick{stroke:var(--gray);stroke-width:1}"
        ".vs-chart .vs-axis{fill:var(--gray);font-size:12px;font-family:inherit}"
        ".vs-chart .vs-label{fill:var(--darkgray);font-size:12px;font-family:inherit}"
        ".vs-chart .vs-cross{stroke:var(--darkgray);stroke-width:1;opacity:0}"
        ".vs-chart .vs-col:hover .vs-cross{opacity:1}"
        ".vs-chart .vs-line{fill:none;stroke:var(--tertiary);stroke-width:2;stroke-linejoin:round}"
        ".vs-chart .vs-avg30{stroke:var(--darkgray)}"
        # Filled areas and bars stay gray until hovered: area charts colour in while the
        # cursor is anywhere over the chart, and the monthly bar chart one bar at a time.
        ".vs-chart .vs-band,.vs-chart .vs-bar,.vs-chart .vs-daybar{fill:var(--gray);transition:fill 0.15s}"
        ".vs-chart .vs-band{stroke:var(--light);stroke-width:2;stroke-linejoin:round}"
        ".vs-chart .vs-daybar{opacity:0.5}"
        ".vs-chart .vs-clipped{opacity:0.8}"
        ".vs-chart:hover .vs-band,.vs-chart:hover .vs-daybar,.vs-chart .vs-barcol:hover .vs-bar{fill:var(--tertiary)}"
        "</style>"
    )


def render_bars(months: list[dict], chart_id: str, title: str) -> str:
    """Bar chart of files per month of last edit, as one line of HTML."""
    bottom = BOTTOM + 14  # room for a year row under the month labels
    counts = [m["files"] for m in months]
    peak = max(max(counts), 1)
    step = nice_step(peak)
    y_max = step * -(-peak // step)
    band = (W - LEFT - RIGHT) / len(months)
    bar = max(band - 6, 1)

    def y(v: float) -> float:
        return TOP + (H - TOP - bottom) * (1 - v / y_max)

    parts: list[str] = []
    for v in range(0, y_max + 1, step):
        parts.append(
            f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="vs-grid"/>'
            f'<text x="{LEFT - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="vs-axis">{tick_label(v)}</text>'
        )
    for i, month in enumerate(months):
        first = date.fromisoformat(month["month"] + "-01")
        center = LEFT + band * (i + 0.5)
        left, base, top = center - bar / 2, y(0), y(month["files"])
        radius = min(4, (base - top) / 2, bar / 2)
        noun = "file" if month["files"] == 1 else "files"
        tip = f"{first:%B %Y}: {month['files']:,} {noun} last edited"
        shape = ""
        if month["files"]:
            # rounded data end at the top, square at the baseline
            shape = (
                f'<path class="vs-bar" d="M{left:.1f},{base:.1f}V{top + radius:.1f}'
                f"Q{left:.1f},{top:.1f} {left + radius:.1f},{top:.1f}"
                f"H{left + bar - radius:.1f}Q{left + bar:.1f},{top:.1f} {left + bar:.1f},{top + radius:.1f}"
                f'V{base:.1f}Z"/>'
            )
        parts.append(
            f'<g class="vs-barcol"><title>{html.escape(tip)}</title>'
            f'<rect x="{center - band / 2:.1f}" y="{TOP}" width="{band:.1f}" height="{H - TOP - bottom}" fill="transparent"/>'
            f"{shape}</g>"
            f'<text x="{center:.1f}" y="{H - bottom + 16}" text-anchor="middle" class="vs-axis">{first:%b}</text>'
        )
        if i == 0 or first.month == 1:
            parts.append(
                f'<text x="{center:.1f}" y="{H - bottom + 30}" text-anchor="middle" class="vs-axis">{first.year}</text>'
            )
    svg = (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-title">'
        f'<title id="{chart_id}-title">{html.escape(title)}</title>'
        + "".join(parts)
        + "</svg>"
    )
    return f'<div class="vs-chart" id="{chart_id}">{svg}</div>'


def spread_labels(positions: list[float], low: float, high: float, gap: float) -> list[float]:
    """Nudge label y positions apart by at least `gap`, keeping them within [low, high].

    `positions` run bottom band first (largest y). Order is preserved.
    """
    placed = list(positions)
    for i in range(1, len(placed)):
        placed[i] = min(placed[i], placed[i - 1] - gap)
    if placed and placed[-1] < low:  # pushed past the top: shift the stack back down
        shift = low - placed[-1]
        placed = [p + shift for p in placed]
    return [min(p, high) for p in placed]


ROW = 24  # height of one horizontal bar row, in SVG pixels
HBAR_W = 480  # narrower than the time charts, so labels stay full size in the page column


def render_hbars(rows: list[dict], chart_id: str, title: str) -> str:
    """Horizontal bar chart of notes per tag, one row per tag, as one line of HTML.

    Names sit on the left, bars grow right with a rounded end, and the count follows the bar.
    The "none" row is a count without a bar, so it doesn't set the scale for the tags.
    """
    label_width = min(max(7 * max(len(r["name"]) for r in rows) + 12, 80), 200)
    left, right = label_width, HBAR_W - 50  # room on the right for the count
    peak = max([r["notes"] for r in rows if r["name"] != "none"] + [1])
    height = TOP + ROW * len(rows) + 4
    parts: list[str] = []
    for i, row in enumerate(rows):
        top = TOP + i * ROW
        bar_height = ROW - 8
        length = 0 if row["name"] == "none" else (right - left) * row["notes"] / peak
        radius = min(4, length / 2, bar_height / 2)
        middle = top + ROW / 2
        noun = "note" if row["notes"] == 1 else "notes"
        shape = ""
        if length:
            y0, y1, x1 = top + 4, top + 4 + bar_height, left + length
            # square at the baseline (left), rounded data end (right)
            shape = (
                f'<path class="vs-bar" d="M{left:.1f},{y0:.1f}H{x1 - radius:.1f}'
                f"Q{x1:.1f},{y0:.1f} {x1:.1f},{y0 + radius:.1f}V{y1 - radius:.1f}"
                f'Q{x1:.1f},{y1:.1f} {x1 - radius:.1f},{y1:.1f}H{left:.1f}Z"/>'
            )
        parts.append(
            f'<g class="vs-barcol"><title>{html.escape(row["name"])}: {row["notes"]:,} {noun}</title>'
            f'<rect x="0" y="{top}" width="{HBAR_W}" height="{ROW}" fill="transparent"/>{shape}</g>'
            f'<text x="{left - 8}" y="{middle + 4:.1f}" text-anchor="end" class="vs-label">{html.escape(row["name"])}</text>'
            f'<text x="{left + length + 6:.1f}" y="{middle + 4:.1f}" class="vs-axis">{row["notes"]:,}</text>'
        )
    svg = (
        f'<svg viewBox="0 0 {HBAR_W} {height}" role="img" aria-labelledby="{chart_id}-title">'
        f'<title id="{chart_id}-title">{html.escape(title)}</title>'
        + "".join(parts)
        + "</svg>"
    )
    return f'<div class="vs-chart" id="{chart_id}">{svg}</div>'


def time_axes(dates: list[date], peak: int):
    """Scales and gridlines for a chart over time: (x(date), y(value), svg parts).

    Y gridlines at a round step; x ticks at the start of each quarter.
    """
    step = nice_step(max(peak, 1))
    y_max = step * -(-max(peak, 1) // step)
    start, end = dates[0], dates[-1]
    span = max((end - start).days, 1)

    def x(d: date) -> float:
        return LEFT + (W - LEFT - RIGHT) * (d - start).days / span

    def y(v: float) -> float:
        return TOP + (H - TOP - BOTTOM) * (1 - v / y_max)

    parts: list[str] = []
    for v in range(0, y_max + 1, step):
        parts.append(
            f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="vs-grid"/>'
            f'<text x="{LEFT - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="vs-axis">{tick_label(v)}</text>'
        )
    tick = date(start.year, 3 * ((start.month - 1) // 3) + 1, 1)
    while tick <= end:
        if tick >= start:
            parts.append(
                f'<line x1="{x(tick):.1f}" x2="{x(tick):.1f}" y1="{H - BOTTOM}" y2="{H - BOTTOM + 4}" class="vs-tick"/>'
                f'<text x="{x(tick):.1f}" y="{H - 8}" text-anchor="middle" class="vs-axis">{tick:%b %Y}</text>'
            )
        tick = date(tick.year + (tick.month == 10), (tick.month + 2) % 12 + 1, 1)
    return x, y, parts


def hover_column(left: float, right: float, center: float, lines: list[str]) -> str:
    """Invisible hover target with a crosshair and a multi-line native tooltip."""
    return (
        # "&#10;" breaks the tooltip lines without breaking the page's one-line HTML block
        f'<g class="vs-col"><title>{"&#10;".join(html.escape(line) for line in lines)}</title>'
        f'<rect x="{left:.1f}" y="{TOP}" width="{max(right - left, 1):.1f}" height="{H - TOP - BOTTOM}" fill="transparent"/>'
        f'<line x1="{center:.1f}" x2="{center:.1f}" y1="{TOP}" y2="{H - BOTTOM}" class="vs-cross"/></g>'
    )


def outlier_cap(days: list[dict]) -> int | None:
    """A y-axis top that cuts off the few biggest days, or None when none stand out.

    The top is the 98th percentile of non-zero days, raised to the peak of the average
    lines if that is higher (so the lines are never cut), then rounded up to a round
    number. It only applies when the biggest day is more than 1.5 times that top, so a
    chart whose biggest days are close to the rest is drawn in full.
    """
    nonzero = sorted(d["count"] for d in days if d["count"])
    if len(nonzero) < 20:
        return None
    averages = [d[f"average_{w}"] or 0 for d in days for w in AVERAGE_WINDOWS]
    target = max(nonzero[int(0.98 * (len(nonzero) - 1))], *averages, 1)
    step = nice_step(target)
    cap = int(step * -(-target // step))
    return cap if nonzero[-1] > 1.5 * cap else None


def render_daily(days: list[dict], unit: str, chart_id: str, title: str) -> str:
    """Thin daily bars with 7- and 30-day average lines over them, as one line of HTML.

    Extreme days (see outlier_cap) are cut off at the top and labelled with their count.
    """
    dates = [date.fromisoformat(d["date"]) for d in days]
    counts = [d["count"] for d in days]
    cap = outlier_cap(days)
    x, y, parts = time_axes(dates, cap or max(counts))
    day_width = (W - LEFT - RIGHT) / max((dates[-1] - dates[0]).days, 1)
    bar = max(day_width - 0.4, 0.6)  # a hairline gap between days
    base = y(0)
    bars, clipped = [], []
    label_right = LEFT - 10.0  # right edge of the previous cut-off label
    for d, c in zip(dates, counts):
        if not c:
            continue
        top = max(y(c), TOP)
        (clipped if cap and c > cap else bars).append(
            f"M{x(d) - bar / 2:.1f},{base:.1f}V{top:.1f}h{bar:.2f}V{base:.1f}Z"
        )
        if cap and c > cap:
            text = f"{c:,}"
            width = 7 * len(text)  # about 7px per character at 12px
            # centred over the bar, but never left of the plot or over the previous label
            left = max(x(d) - width / 2, LEFT, label_right + 6)
            label_right = left + width
            parts.append(f'<text x="{left:.1f}" y="{TOP - 3}" class="vs-label">{text}</text>')
    parts.append(f'<path d="{"".join(bars)}" class="vs-daybar"/>')
    if clipped:
        parts.append(f'<path d="{"".join(clipped)}" class="vs-daybar vs-clipped"/>')
    labels: list[tuple[float, str]] = []
    for window in AVERAGE_WINDOWS:
        key = f"average_{window}"
        averaged = [(d, day[key]) for d, day in zip(dates, days) if day[key] is not None]
        if not averaged:
            continue
        points = " ".join(f"{x(d):.1f},{max(y(a), TOP):.1f}" for d, a in averaged)
        # 7-day in the accent colour; 30-day in the text colour, so the two lines differ
        parts.append(f'<polyline points="{points}" class="vs-line vs-avg{window}"/>')
        labels.append((max(y(averaged[-1][1]), TOP) + 4, f"{window}-day avg"))
    labels.sort(reverse=True)  # bottom first, as spread_labels expects
    for label_y, (_, text) in zip(
        spread_labels([ly for ly, _ in labels], TOP + 10, H - BOTTOM, LABEL_GAP), labels
    ):
        parts.append(f'<text x="{W - RIGHT + 6}" y="{label_y:.1f}" class="vs-label">{text}</text>')
    for k, d in enumerate(dates):
        left = LEFT if k == 0 else (x(dates[k - 1]) + x(d)) / 2
        right = W - RIGHT if k == len(dates) - 1 else (x(d) + x(dates[k + 1])) / 2
        noun = unit[:-1] if counts[k] == 1 else unit
        so_far = " (so far)" if k == len(dates) - 1 else ""
        lines = [f"{d:%a} {d:%b} {d.day}, {d.year}{so_far}: {counts[k]:,} {noun}"]
        for window in AVERAGE_WINDOWS:
            if days[k][f"average_{window}"] is not None:
                lines.append(f"{window}-day average: {days[k][f'average_{window}']:,}")
        parts.append(hover_column(left, right, x(d), lines))
    svg = (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-title">'
        f'<title id="{chart_id}-title">{html.escape(title)}</title>'
        + "".join(parts)
        + "</svg>"
    )
    return f'<div class="vs-chart" id="{chart_id}">{svg}</div>'


def render_chart(weekly: list[dict], quarters: list[str], unit: str, chart_id: str, title: str) -> str:
    """Stacked area chart, oldest quarter at the bottom, as one line of HTML.

    Every band has the same fill; white (or black) separator lines split them, and
    each band still standing today is labelled at the right edge.
    """
    shown = [q for q in quarters if any(w["counts"].get(q) for w in weekly)]
    dates = [date.fromisoformat(w["date"]) for w in weekly]
    totals = [sum(w["counts"].values()) for w in weekly]
    x, y, parts = time_axes(dates, max(totals))

    lower = [0] * len(weekly)
    label_quarters: list[str] = []
    label_ys: list[float] = []
    for quarter in shown:
        upper = [lo + w["counts"].get(quarter, 0) for lo, w in zip(lower, weekly)]
        top = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in zip(dates, upper))
        bottom = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in reversed(list(zip(dates, lower))))
        parts.append(
            f'<polygon points="{top} {bottom}" class="vs-band">'
            f"<title>{html.escape(quarter_label(quarter, quarters[0]))}</title></polygon>"
        )
        if upper[-1] > lower[-1]:  # the band still has files or lines today
            label_quarters.append(quarter)
            label_ys.append(y((upper[-1] + lower[-1]) / 2) + 4)
        lower = upper

    for quarter, label_y in zip(
        label_quarters, spread_labels(label_ys, TOP + 10, H - BOTTOM, LABEL_GAP)
    ):
        parts.append(
            f'<text x="{W - RIGHT + 6}" y="{label_y:.1f}" class="vs-label">{html.escape(quarter)}</text>'
        )

    # hover columns: crosshair plus a native tooltip with the week's breakdown
    for k, d in enumerate(dates):
        left = LEFT if k == 0 else (x(dates[k - 1]) + x(d)) / 2
        right = W - RIGHT if k == len(dates) - 1 else (x(d) + x(dates[k + 1])) / 2
        noun = unit[:-1] if totals[k] == 1 else unit
        lines = [f"Week ending {d:%b} {d.day}, {d.year}: {totals[k]:,} {noun}"]
        lines += [f"{q}: {weekly[k]['counts'][q]:,}" for q in reversed(shown) if weekly[k]["counts"].get(q)]
        parts.append(hover_column(left, right, x(d), lines))

    svg = (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-title">'
        f'<title id="{chart_id}-title">{html.escape(title)}, {dates[0]:%b %Y} to {dates[-1]:%b %Y}</title>'
        + "".join(parts)
        + "</svg>"
    )
    return f'<div class="vs-chart" id="{chart_id}">{svg}</div>'


INDENT = "&emsp;"  # indents a nested row in the summary table


def render_page(stats: dict) -> str:
    generated = datetime.fromisoformat(stats["generated"])
    quarters = stats["quarters"]

    def percent(part: int, whole: int) -> str:
        return f"{100 * part / whole:.1f}%" if whole else "0%"

    return "\n".join(
        [
            "---",
            "publish: true",
            "title: Garage stats",
            "tags:",
            "  - topic/garage",
            "  - type/system",
            "---",
            f"*Last updated: {generated:%Y-%m-%d %H:%M} {generated.tzname()}*",
            "",
            "## Summary table",
            "",
            "| Stat | Value |",
            "|---|---|",
            f"| Files[^files] | {stats['files']:,} |",
            f"| {INDENT}Markdown notes | {stats['notes']:,} ({percent(stats['notes'], stats['files'])}) |",
            f"| {INDENT * 2}Syndicated to the door (this site)[^syndicated] | {stats['public_notes']:,}"
            f" ({percent(stats['public_notes'], stats['notes'])}) |",
            f"| {INDENT * 2}Syndicated to AI agents | {stats['ai_notes']:,}"
            f" ({percent(stats['ai_notes'], stats['notes'])}) |",
            f"| Files added per day[^rate] | {stats['files_per_day']:,} |",
            f"| Lines in notes | {stats['lines']:,} |",
            f"| Commits | {stats['commits']:,} |",
            "",
            chart_style(),
            "",
            "## Notes by tag[^tags]",
            "",
            "### Type",
            "",
            render_hbars(stats["tags"]["type"], "vs-tags-type", "Notes by first-level type tag"),
            "",
            "### Topic",
            "",
            render_hbars(stats["tags"]["topic"], "vs-tags-topic", "Notes by first-level topic tag"),
            "",
            "## Over time graphs",
            "",
            "### New files[^new-files]",
            "",
            render_daily(stats["daily_new_files"], "files", "vs-new-files", "New vault files per day, with 7-day and 30-day averages"),
            "",
            "### New lines in notes[^new-lines]",
            "",
            render_daily(stats["daily_new_lines"], "lines", "vs-new-lines", "New lines in notes per day, with 7-day and 30-day averages"),
            "",
            "### Commits[^commits]",
            "",
            render_daily(stats["daily_commits"], "commits", "vs-commits", "Vault commits per day, with 7-day and 30-day averages"),
            "",
            "### Files by when they were last edited[^edited]",
            "",
            render_bars(stats["last_edited_months"], "vs-edited", "Vault files by the month they were last edited"),
            "",
            "## By quarter graphs",
            "",
            "### Files[^files-quarter]",
            "",
            render_chart(stats["weekly_files"], quarters, "files", "vs-files", "Vault files by the quarter they were added"),
            "",
            "### Lines in notes[^lines-quarter]",
            "",
            render_chart(stats["weekly_lines"], quarters, "lines", "vs-lines", "Lines in notes by the quarter they were written"),
            "",
            "[^files]: Everything tracked in the vault's git repository, except hidden tool folders such as `.obsidian`.",
            "[^syndicated]: Every note starts private. Syndication shares are out of Markdown notes.",
            "[^tags]: First-level tags only, with `task` counted as `type/task`. Only tags that appear on"
            " published notes are named; notes with any other tag are counted once as other. A note"
            " with several tags counts under each; none is notes without a tag of that kind.",
            f"[^rate]: Net change in files since the first commit on {stats['first_commit']}"
            f" ({stats['first_commit_files']:,} files), per day.",
            "[^new-files]: Bars are daily counts. The orange line is the 7-day average, the dark line the"
            " 30-day. Today is partial. Hover any graph for exact numbers. Deleted files still count on the"
            f" day they were added; renames don't count.{clip_note(stats['daily_new_files'])}",
            "[^new-lines]: Lines written in notes. Edits count as new lines; deletions aren't subtracted."
            f"{clip_note(stats['daily_new_lines'])}",
            f"[^commits]: All commits, merges included.{clip_note(stats['daily_commits'])}",
            "[^edited]: Each file once, in the month of its last commit. Pure renames aren't edits.",
            "[^files-quarter]: Files still present, by the quarter they were added, oldest at the bottom."
            f" Renames keep their quarter. {quarters[0]} is mostly the initial import.",
            "[^lines-quarter]: Lines still present, by the quarter they were written. Editing a line moves it to that quarter.",
            "",
        ]
    )


def clip_note(days: list[dict]) -> str:
    """Footnote sentence listing a daily chart's cut-off days, else empty."""
    cap = outlier_cap(days)
    if cap is None:
        return ""
    over = [d for d in days if d["count"] > cap]
    listed = "; ".join(
        f"{date.fromisoformat(d['date']):%b} {date.fromisoformat(d['date']).day}, {d['date'][:4]} ({d['count']:,})"
        for d in over
    )
    return f" Cut off at the top: {listed}."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault-path", required=True, type=Path)
    args = parser.parse_args(argv)
    vault: Path = args.vault_path.resolve()

    if (vault / ".obsidian" / "workspace.json").exists():
        print(
            f"Refusing to write into {vault}: it looks like a live Obsidian vault"
            " (.obsidian/workspace.json exists). Run this against a clone.",
            file=sys.stderr,
        )
        return 2

    stats = collect_stats(vault, datetime.now(TZ))
    (vault / PAGE_NAME).write_text(render_page(stats), encoding="utf-8")
    (vault / DATA_NAME).write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {PAGE_NAME}: {stats['files']:,} files, {stats['lines']:,} lines,"
        f" {stats['commits']:,} commits, {stats['public_notes']} public and {stats['ai_notes']}"
        f" syndicate: ai of {stats['notes']:,} notes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
