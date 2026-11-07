"""Tests for vault-stats.py, using throwaway git repositories.

Run from the door folder:  python -m unittest -v scripts/test_vault_stats.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "vault_stats", Path(__file__).with_name("vault-stats.py")
)
vs = importlib.util.module_from_spec(SPEC)
sys.modules["vault_stats"] = vs  # dataclasses look the module up while it loads
SPEC.loader.exec_module(vs)


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, when=None):
        env = dict(os.environ)
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=env)

    def write(self, path, text=None):
        # distinct default contents, so git doesn't pair unrelated files as renames
        text = f"content of {path}\n" if text is None else text
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def commit(self, when, message="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, when=when)


class VaultStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self.tmp.name))
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=vs.TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def stats(self):
        return vs.collect_stats(self.repo.root, self.now)

    # ---------- files ----------

    def test_adds_renames_deletes_and_hidden_files(self):
        r = self.repo
        r.write("a.md")
        r.write("b.png")
        r.commit("2026-01-15T10:00:00-06:00")
        (r.root / "folder").mkdir()
        r.git("mv", "a.md", "folder/a.md")
        (r.root / "b.png").unlink()
        r.write("c.md")
        r.write(".obsidian/app.json", "{}")
        r.write(".gitignore", "x")
        r.commit("2026-04-10T10:00:00-05:00")

        stats = self.stats()
        self.assertEqual(stats["files"], 2)
        # the renamed note keeps its Q1 cohort; the deleted image is gone
        self.assertEqual(stats["weekly_files"][-1]["counts"], {"2026 Q1": 1, "2026 Q2": 1})
        self.assertEqual(stats["commits"], 2)

    def test_merge_is_not_double_counted(self):
        r = self.repo
        r.write("base.md")
        r.commit("2026-02-01T10:00:00-06:00")
        r.git("checkout", "-q", "-b", "side")
        r.write("side.md")
        r.commit("2026-02-02T10:00:00-06:00")
        r.git("checkout", "-q", "main")
        r.write("main.md")
        r.commit("2026-02-03T10:00:00-06:00")
        r.git("merge", "-q", "--no-ff", "side", "-m", "merge", when="2026-02-04T10:00:00-06:00")

        stats = self.stats()  # also checks the line replay through the merge
        self.assertEqual(stats["files"], 3)
        self.assertEqual(stats["weekly_files"][-1]["counts"], {"2026 Q1": 3})
        self.assertEqual(stats["lines"], 3)

    def test_quarter_uses_chicago_time(self):
        r = self.repo
        # 04:30 UTC on Apr 1 is 23:30 on Mar 31 in Chicago
        r.write("late.md")
        r.commit("2026-04-01T04:30:00+00:00")
        self.assertEqual(self.stats()["weekly_files"][-1]["counts"], {"2026 Q1": 1})

    def test_unicode_names_survive_renames(self):
        r = self.repo
        r.write("Café ’note’.md")
        r.commit("2026-03-01T10:00:00-06:00")
        r.git("mv", "Café ’note’.md", "Crème brûlée.md")
        r.commit("2026-05-01T10:00:00-05:00")
        stats = self.stats()  # raises if either replay doesn't match HEAD
        self.assertEqual(stats["weekly_files"][-1]["counts"], {"2026 Q1": 1})
        self.assertEqual(stats["weekly_lines"][-1]["counts"], {"2026 Q1": 1})

    def test_last_edited_months(self):
        r = self.repo
        body = "".join(f"line {i}\n" for i in range(20))
        r.write("edited.md", body)
        r.write("untouched.md", "u\n" + body)
        r.write("moved.md", "m\n" + body)
        r.write("renamed-and-edited.md", "r\n" + body)
        r.commit("2026-01-15T10:00:00-06:00")
        r.write("edited.md", body + "more\n")  # an edit
        r.commit("2026-03-10T10:00:00-06:00")
        (r.root / "folder").mkdir()
        r.git("mv", "moved.md", "folder/moved.md")  # pure rename: not an edit
        r.commit("2026-04-10T10:00:00-05:00")
        r.git("mv", "renamed-and-edited.md", "folder/renamed-and-edited.md")
        r.write("folder/renamed-and-edited.md", "r\n" + body + "changed\n")  # rename plus edit
        r.commit("2026-05-10T10:00:00-05:00")
        r.write("gone.md")
        r.commit("2026-06-10T10:00:00-05:00")
        (r.root / "gone.md").unlink()  # deleted files drop out
        r.commit("2026-07-10T10:00:00-05:00")

        months = {m["month"]: m["files"] for m in self.stats()["last_edited_months"]}
        self.assertEqual(list(months)[0], "2026-01")
        self.assertEqual(list(months)[-1], "2026-11")  # through the current month
        self.assertEqual(months["2026-01"], 2)  # untouched, moved
        self.assertEqual(months["2026-03"], 1)  # edited
        self.assertEqual(months["2026-05"], 1)  # renamed and edited
        self.assertEqual(months["2026-06"] + months["2026-07"], 0)
        self.assertEqual(sum(months.values()), 4)

    def test_daily_commits_and_seven_day_average(self):
        r = self.repo
        r.write("a.md")
        r.commit("2026-10-01T10:00:00-05:00")
        r.git("checkout", "-q", "-b", "side")
        r.write("b.md")
        r.commit("2026-10-02T09:00:00-05:00")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--no-ff", "side", "-m", "merge", when="2026-10-02T10:00:00-05:00")
        r.write("c.md")
        # 04:30 UTC on Oct 10 is 23:30 on Oct 9 in Chicago
        r.commit("2026-10-10T04:30:00+00:00")
        stats = self.stats()
        days = {d["date"]: d for d in stats["daily_commits"]}
        self.assertEqual(list(days)[0], "2026-10-01")
        self.assertEqual(list(days)[-1], "2026-11-07")  # through today
        self.assertEqual(
            [days[f"2026-10-{n:02d}"]["count"] for n in range(1, 11)],
            [1, 2, 0, 0, 0, 0, 0, 0, 1, 0],  # merge commit included; empty days are zero
        )
        self.assertIsNone(days["2026-10-06"]["average_7"])  # no full 7-day window yet
        self.assertEqual(days["2026-10-07"]["average_7"], round(3 / 7, 1))
        self.assertEqual(days["2026-10-09"]["average_7"], round(1 / 7, 1))
        self.assertIsNone(days["2026-10-29"]["average_30"])  # day 29: no full 30-day window
        self.assertEqual(days["2026-10-30"]["average_30"], round(4 / 30, 1))  # Oct 1-30
        self.assertEqual(days["2026-10-31"]["average_30"], round(3 / 30, 1))  # Oct 2-31
        self.assertEqual(sum(d["count"] for d in days.values()), stats["commits"])

    def test_new_files_per_day(self):
        r = self.repo
        r.write("a.md")
        r.write("b.md")
        r.write(".obsidian/app.json", "{}")  # hidden: not a new vault file
        r.commit("2026-10-01T10:00:00-05:00")
        (r.root / "folder").mkdir()
        r.git("mv", "a.md", "folder/a.md")  # a rename isn't new
        r.write("c.md")
        r.commit("2026-10-02T10:00:00-05:00")
        (r.root / "c.md").unlink()  # deleted later, but still new on Oct 2
        r.commit("2026-10-03T10:00:00-05:00")
        r.git("mv", ".obsidian/app.json", "app.json")  # moved out of a hidden folder: newly counted
        r.commit("2026-10-04T10:00:00-05:00")
        days = {d["date"]: d["count"] for d in self.stats()["daily_new_files"]}
        self.assertEqual(
            [days[f"2026-10-0{n}"] for n in range(1, 6)], [2, 1, 0, 1, 0]
        )
        self.assertEqual(list(days)[-1], "2026-11-07")

    def test_outlier_cap_cuts_off_only_extreme_days(self):
        def series(counts):
            return [
                {"date": f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}", "count": c, "average_7": None, "average_30": None}
                for i, c in enumerate(counts)
            ]

        ordinary = [10, 12, 8, 15, 9, 11, 14, 10, 13, 12] * 3
        self.assertIsNone(vs.outlier_cap(series(ordinary)))  # nothing extreme
        self.assertIsNone(vs.outlier_cap(series(ordinary + [20])))  # busy, not standing out
        # a handful of days about 3x the rest, as in the new-lines chart, are cut off
        self.assertEqual(vs.outlier_cap(series(ordinary * 5 + [40, 45, 50])), 15)
        cap = vs.outlier_cap(series(ordinary + [1085]))
        self.assertIsNotNone(cap)
        self.assertTrue(15 <= cap < 1085 / 2)
        note = vs.clip_note(series(ordinary + [1085]))
        self.assertIn("(1,085)", note)
        self.assertTrue(note.startswith(" Cut off at the top: Feb "))

    def test_files_per_day_is_net_change_since_first_commit(self):
        r = self.repo
        for i in range(10):
            r.write(f"old{i}.md")
        r.commit("2026-08-01T10:00:00-05:00")
        for i in range(4):
            (r.root / f"old{i}.md").unlink()
        for i in range(10):
            r.write(f"new{i}.md")
        r.commit("2026-10-30T10:00:00-05:00")
        stats = self.stats()
        first = datetime.fromisoformat("2026-08-01T10:00:00-05:00")
        days = (self.now - first).total_seconds() / 86400
        self.assertEqual(stats["files"], 16)
        self.assertEqual(stats["first_commit_files"], 10)
        self.assertEqual(stats["files_per_day"], round(6 / days, 1))

    def test_weekly_samples_end_today(self):
        r = self.repo
        r.write("a.md")
        r.commit("2026-10-01T10:00:00-05:00")
        weekly = self.stats()["weekly_files"]
        self.assertEqual(weekly[-1]["date"], "2026-11-07")
        dates = [datetime.fromisoformat(w["date"]) for w in weekly[:-1]]
        self.assertTrue(all(d.weekday() == 6 for d in dates))  # Sundays
        self.assertTrue(all(b - a == timedelta(days=7) for a, b in zip(dates, dates[1:])))

    # ---------- lines ----------

    def test_lines_keep_the_quarter_they_were_written(self):
        r = self.repo
        r.write("note.md", "a\nb\nc\n")
        r.write("data.txt", "1\n2\n3\n4\n")  # not a note: no lines counted
        r.write(".secret/hidden.md", "x\ny\n")  # hidden: no lines counted
        r.commit("2026-02-01T10:00:00-06:00")
        r.write("note.md", "a\nB\nc\nd\n")  # edit line 2, append line 4
        r.commit("2026-05-01T10:00:00-05:00")
        r.write("note.md", "B\nc\nd\n")  # delete line 1
        r.git("add", "-A")
        (r.root / "folder").mkdir()
        r.git("mv", "note.md", "folder/note.md")  # a rename keeps every line's quarter
        r.commit("2026-08-01T10:00:00-05:00")
        stats = self.stats()  # raises if the replayed line counts don't match HEAD
        self.assertEqual(stats["lines"], 3)
        self.assertEqual(stats["weekly_lines"][-1]["counts"], {"2026 Q1": 1, "2026 Q2": 2})

    def test_new_lines_per_day(self):
        r = self.repo
        r.write("note.md", "a\nb\nc\n")
        r.write("data.txt", "1\n2\n")  # not a note
        r.write(".secret/hidden.md", "x\n")  # hidden
        r.commit("2026-10-01T10:00:00-05:00")
        r.write("note.md", "a\nB\nc\nd\n")  # one edited line, one appended
        r.commit("2026-10-02T10:00:00-05:00")
        r.write("note.md", "B\nc\nd\n")  # a deletion writes nothing
        r.commit("2026-10-03T10:00:00-05:00")
        (r.root / "folder").mkdir()
        r.git("mv", "note.md", "folder/note.md")  # a pure rename writes nothing
        r.commit("2026-10-04T10:00:00-05:00")
        days = {d["date"]: d["count"] for d in self.stats()["daily_new_lines"]}
        self.assertEqual([days[f"2026-10-0{n}"] for n in range(1, 5)], [3, 2, 0, 0])

    def test_lines_without_final_newline(self):
        r = self.repo
        r.write("n.md", "one\ntwo")
        r.commit("2026-02-01T10:00:00-06:00")
        r.write("n.md", "one\ntwo\nthree")
        r.commit("2026-05-01T10:00:00-05:00")
        stats = self.stats()
        self.assertEqual(stats["lines"], 3)
        # "two" gains a newline, so git rewrites it: it moves to Q2 along with "three"
        self.assertEqual(stats["weekly_lines"][-1]["counts"], {"2026 Q1": 1, "2026 Q2": 2})

    def test_lines_of_deleted_and_empty_notes(self):
        r = self.repo
        r.write("gone.md", "1\n2\n")
        r.write("empty.md", "")
        r.commit("2026-02-01T10:00:00-06:00")
        (r.root / "gone.md").unlink()
        r.commit("2026-03-01T10:00:00-06:00")
        stats = self.stats()
        self.assertEqual(stats["lines"], 0)
        self.assertEqual(stats["files"], 1)

    def test_unquote_and_header_paths(self):
        self.assertEqual(vs.unquote_path('"caf\\303\\251 \\"x\\".md"'), 'café "x".md')
        self.assertEqual(vs.unquote_path("plain name.md"), "plain name.md")
        self.assertEqual(vs.header_paths("a/x y.md b/x y.md"), ("x y.md", "x y.md"))
        self.assertEqual(vs.header_paths('"a/caf\\303\\251.md" "b/caf\\303\\251.md"'), ("café.md", "café.md"))

    # ---------- public notes and page ----------

    def test_tag_counts_name_only_tags_on_published_notes(self):
        r = self.repo
        r.write("pub-ref.md", "---\npublish: true\ntags:\n  - type/reference/website\n  - topic/garage\n---\n")
        r.write("pub-task.md", '---\npublish: true\ntags: ["#Task/review"]\n---\n')
        r.write("ref.md", "---\ntags:\n  - type/reference\n  - topic/secret-project/plan\n---\n")
        r.write("two.md", "---\ntags: [type/reference, type/diary]\n---\n")  # diary is private
        r.write("task.md", "---\ntags: task\n---\n")
        r.write("plain.md", "no frontmatter\n")
        r.write("other-key.md", "---\ntags:\n  - topic/garage\naliases:\n  - type/not-a-tag\n---\n")
        r.write(".secret/hidden.md", "---\npublish: true\ntags:\n  - type/hidden\n---\n")
        r.commit("2026-06-01T10:00:00-05:00")
        stats = self.stats()
        types = {row["name"]: row["notes"] for row in stats["tags"]["type"]}
        topics = {row["name"]: row["notes"] for row in stats["tags"]["topic"]}
        # pub-ref, ref, two (each counts once per named tag)
        self.assertEqual(types["type/reference"], 3)
        self.assertEqual(types["type/task"], 2)  # pub-task (#Task/review) and task
        self.assertEqual(types["other"], 1)  # two.md, for type/diary
        self.assertEqual(types["none"], 2)  # plain, other-key
        self.assertEqual(topics, {"topic/garage": 2, "other": 1, "none": 4})
        # private tags are never named, in the page or the published JSON
        self.assertEqual(list(types)[-2:], ["other", "none"])
        published = vs.render_page(stats) + json.dumps(stats)
        for private in ("diary", "secret-project", "type/hidden", "not-a-tag"):
            self.assertNotIn(private, published)
        self.assertIn("type/reference", published)

    def test_task_is_named_only_when_a_published_note_has_it(self):
        r = self.repo
        r.write("pub.md", "---\npublish: true\ntags:\n  - type/reference\n---\n")
        r.write("task.md", "---\ntags:\n  - task\n---\n")
        r.commit("2026-06-01T10:00:00-05:00")
        types = {row["name"]: row["notes"] for row in self.stats()["tags"]["type"]}
        self.assertNotIn("type/task", types)
        self.assertEqual(types["other"], 1)

    def test_first_level_and_tag_block_parsing(self):
        self.assertEqual(vs.first_level("type/reference/website/clipping"), "type/reference")
        self.assertEqual(vs.first_level("#Topic/Garage"), "topic/garage")
        self.assertEqual(vs.first_level("task/review"), "type/task")
        self.assertIsNone(vs.first_level("type"))
        self.assertIsNone(vs.first_level("origin/ai"))
        self.assertEqual(vs.tags_from_block("tags:\n  - a\n  - 'b'\ntitle: x"), ["a", "'b'"])
        self.assertEqual(vs.tags_from_block("title: x\ntags: [a, b]"), ["a", " b"])
        self.assertEqual(vs.tags_from_block("tags: a"), ["a"])
        self.assertEqual(vs.tags_from_block("title: x"), [])

    def test_syndicated_notes(self):
        r = self.repo
        r.write("yes.md", "---\npublish: true\n---\nhi\n")
        r.write("string.md", '---\npublish: "true"\n---\nhi\n')
        r.write("no.md", "---\npublish: false\n---\nhi\n")
        r.write("plain.md", "no frontmatter\n")
        r.write("broken.md", "---\n: : bad yaml [\n---\n")
        r.write("ai-list.md", "---\ntags:\n  - task\nsyndicate:\n  - ai\n---\nhi\n")
        r.write("ai-value.md", "---\nsyndicate: ai\n---\nhi\n")
        r.write("other.md", "---\nsyndicate:\n  - neocities\n---\nhi\n")
        r.write("both.md", "---\npublish: true\nsyndicate:\n  - ai\n---\nhi\n")
        r.write(".secret/hidden.md", "---\npublish: true\nsyndicate: ai\n---\n")
        r.write("image.png")
        r.commit("2026-06-01T10:00:00-05:00")
        stats = self.stats()
        self.assertEqual(stats["notes"], 9)
        self.assertEqual(stats["files"], 10)
        self.assertEqual(stats["public_notes"], 3)  # yes, string, both
        self.assertEqual(stats["ai_notes"], 3)  # ai-list, ai-value, both

    def test_page_frontmatter_and_html_blocks(self):
        r = self.repo
        r.write("a.md", "---\npublish: true\n---\n")
        r.commit("2026-01-15T10:00:00-06:00")
        r.write("b.md")
        r.commit("2026-07-15T10:00:00-05:00")
        page = vs.render_page(self.stats())
        self.assertTrue(page.startswith("---\npublish: true\n"))
        self.assertIn("| Lines in notes |", page)
        # nested summary rows: notes as a share of files, syndication as a share of notes
        self.assertIn(
            "| Files[^files] | 2 |\n"
            "| &emsp;Markdown notes | 2 (100.0%) |\n"
            "| &emsp;&emsp;Syndicated to the door (this site)[^syndicated] | 1 (50.0%) |\n"
            "| &emsp;&emsp;Syndicated to AI agents | 0 (0.0%) |\n"
            "| Files added per day[^rate] | 0.0 |\n"
            "| Lines in notes | 4 |\n"
            "| Commits | 2 |",
            page,
        )
        headings = [line for line in page.splitlines() if line.startswith("#")]
        self.assertEqual(
            headings,
            [
                "## Summary table",
                "## Notes by tag[^tags]",
                "### Type",
                "### Topic",
                "## Over time graphs",
                "### New files[^new-files]",
                "### New lines in notes[^new-lines]",
                "### Commits[^commits]",
                "### Files by when they were last edited[^edited]",
                "## By quarter graphs",
                "### Files[^files-quarter]",
                "### Lines in notes[^lines-quarter]",
            ],
        )
        # every footnote reference has a definition, and vice versa
        refs = set(re.findall(r"\[\^([\w-]+)\](?!:)", page))
        definitions = set(re.findall(r"^\[\^([\w-]+)\]:", page, re.MULTILINE))
        self.assertEqual(refs, definitions)
        self.assertEqual(len(definitions), 10)
        self.assertNotIn("<details>", page)
        lines = [line for line in page.splitlines() if line.startswith('<div class="vs-chart"')]
        charts = {re.search(r'id="([^"]+)"', line).group(1): line for line in lines}
        # each chart must be one line: a blank line would end the HTML block in Markdown
        self.assertEqual(
            list(charts),
            [
                "vs-tags-type",
                "vs-tags-topic",
                "vs-new-files",
                "vs-new-lines",
                "vs-commits",
                "vs-edited",
                "vs-files",
                "vs-lines",
            ],
        )  # tags, then over-time graphs, then the by-quarter graphs
        for chart in charts.values():
            self.assertTrue(chart.endswith("</svg></div>"))
        for chart_id in ("vs-files", "vs-lines"):
            self.assertIn("2026 Q1 (initial import)", charts[chart_id])
        self.assertIn("January 2026: 1 file last edited", charts["vs-edited"])
        self.assertIn("July 2026: 1 file last edited", charts["vs-edited"])
        self.assertIn("(so far): 0 commits", charts["vs-commits"])
        self.assertIn("7-day avg", charts["vs-commits"])
        self.assertIn("30-day avg", charts["vs-commits"])
        self.assertIn("(so far): 0 files", charts["vs-new-files"])
        self.assertIn("(so far): 0 lines", charts["vs-new-lines"])
        style = next(line for line in page.splitlines() if line.startswith("<style>"))
        self.assertNotIn('"', style)  # Quartz escapes quotes inside <style>
        # fills are gray until hovered, then take the site's accent colour
        self.assertIn(".vs-chart .vs-band,.vs-chart .vs-bar,.vs-chart .vs-daybar{fill:var(--gray)", style)
        self.assertIn(".vs-chart:hover .vs-band", style)
        self.assertIn(".vs-chart .vs-barcol:hover .vs-bar", style)

    def test_band_labels_never_overlap(self):
        # bottom band first; three bands squeezed together near the top
        placed = vs.spread_labels([300.0, 40.0, 35.0, 30.0], 20.0, 292.0, 13.0)
        self.assertEqual(placed[0], 292.0)  # clamped to the plot bottom
        self.assertTrue(all(a - b >= 13.0 for a, b in zip(placed, placed[1:])))
        self.assertGreaterEqual(min(placed), 20.0)

    def test_refuses_live_vault(self):
        self.repo.write(".obsidian/workspace.json", "{}")
        self.assertEqual(vs.main(["--vault-path", str(self.repo.root)]), 2)
        self.assertFalse((self.repo.root / vs.PAGE_NAME).exists())

    def test_main_writes_page_and_json(self):
        r = self.repo
        r.write("a.md")
        r.commit("2026-01-15T10:00:00-06:00")
        self.assertEqual(vs.main(["--vault-path", str(r.root)]), 0)
        self.assertTrue((r.root / vs.PAGE_NAME).exists())
        self.assertTrue((r.root / vs.DATA_NAME).exists())

    def test_own_output_is_not_counted_even_if_tracked(self):
        r = self.repo
        r.write("a.md")
        r.write(vs.PAGE_NAME, "")
        r.commit("2026-01-15T10:00:00-06:00")
        self.assertEqual(vs.main(["--vault-path", str(r.root)]), 0)
        # a second run sees the generated page (publish: true) but must not count it
        stats = self.stats()
        self.assertEqual((stats["files"], stats["notes"], stats["public_notes"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
