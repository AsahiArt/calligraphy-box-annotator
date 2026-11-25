"""The .apkg back migration touches model CSS and afmt only."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import genanki

from tools.patch_apkg_back import DEFAULT_BACK, DEFAULT_CSS, DEFAULT_MODEL, PatchError, patch_apkg

FIELDS = [
    "Simplified",
    "Traditional",
    "Rank",
    "Same",
    "CursiveChar",
    "ImgSun",
    "ImgMifu",
    "ImgHuaisu",
]
MODEL_ID = 1607392319
DECK_ID = 1556553358


def _snapshot(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        media = archive.read("media")
        raw = archive.read("collection.anki2")
    db = path.with_suffix(".snapshot.anki2")
    db.write_bytes(raw)
    try:
        conn = sqlite3.connect(db)
        models = json.loads(conn.execute("select models from col").fetchone()[0])
        decks = json.loads(conn.execute("select decks from col").fetchone()[0])
        notes = conn.execute("select guid, flds, mid from notes order by guid").fetchall()
        cards = conn.execute(
            "select nid, did, ord, type, queue, due, ivl, factor, reps, lapses, left, odue, odid from cards order by nid"
        ).fetchall()
        conn.close()
    finally:
        db.unlink(missing_ok=True)
    model = models[str(MODEL_ID)]
    return {
        "media": media,
        "model_id": model["id"],
        "model_name": model["name"],
        "qfmt": model["tmpls"][0]["qfmt"],
        "afmt": model["tmpls"][0]["afmt"],
        "css": model["css"],
        "deck_id": decks[str(DECK_ID)]["id"],
        "notes": notes,
        "cards": cards,
    }


class PatchApkgTests(unittest.TestCase):
    def test_back_and_css_change_without_touching_notes_or_scheduling(self) -> None:
        model = genanki.Model(
            MODEL_ID,
            DEFAULT_MODEL,
            fields=[{"name": name} for name in FIELDS],
            templates=[
                {
                    "name": "草书",
                    "qfmt": '<div class="front">{{Simplified}}</div>',
                    "afmt": '<div class="old-back">{{ImgMifu}}</div>',
                }
            ],
            css=".old { color: red; }",
        )
        note = genanki.Note(
            model=model,
            fields=["书", "書", "3", "0", "书", "<img src=\"sun.jpg\">", "mifu.jpg", "huai.jpg"],
            guid=genanki.guid_for("书谱", "书"),
        )
        deck = genanki.Deck(DECK_ID, "书谱")
        deck.add_note(note)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "shupu.apkg"
            dest = folder / "shupu-back.apkg"
            genanki.Package(deck).write_to_file(str(source), timestamp=1700000000)
            before = _snapshot(source)
            names = patch_apkg(
                source,
                dest,
                css=DEFAULT_CSS.read_text(encoding="utf-8"),
                afmt=DEFAULT_BACK.read_text(encoding="utf-8").strip() + "\n",
                model_name=DEFAULT_MODEL,
            )
            self.assertEqual(names, [DEFAULT_MODEL])
            after = _snapshot(dest)
            self.assertEqual(after["notes"], before["notes"])
            self.assertEqual(after["cards"], before["cards"])
            self.assertEqual(after["media"], before["media"])
            self.assertEqual(after["model_id"], before["model_id"])
            self.assertEqual(after["model_id"], str(MODEL_ID))
            self.assertEqual(after["deck_id"], before["deck_id"])
            self.assertEqual(after["qfmt"], before["qfmt"])
            self.assertNotEqual(after["afmt"], before["afmt"])
            self.assertIn("{{Simplified}}", after["afmt"])
            self.assertIn("{{ImgSun}}", after["afmt"])
            self.assertIn("{{ImgMifu}}", after["afmt"])
            self.assertIn("{{ImgHuaisu}}", after["afmt"])
            self.assertIn("{{CursiveChar}}", after["afmt"])
            self.assertNotIn("{{Rank}}", after["afmt"])
            self.assertNotIn("{{Char}}", after["afmt"])
            self.assertIn("--paper: #1a1714", after["css"])
            self.assertIn("flex: 0 0 auto", after["css"])
            self.assertNotIn("grid-template-rows: minmax(0, 1fr) minmax(0, 1fr)", after["css"])
            self.assertTrue(source.is_file())
            self.assertEqual(_snapshot(source)["afmt"], before["afmt"])

    def test_unknown_model_writes_nothing(self) -> None:
        model = genanki.Model(
            42,
            "Other",
            fields=[{"name": "Char"}],
            templates=[{"name": "Card", "qfmt": "{{Char}}", "afmt": "{{Char}}"}],
        )
        deck = genanki.Deck(99, "Other")
        deck.add_note(genanki.Note(model=model, fields=["甲"], guid=genanki.guid_for("other", "甲")))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "other.apkg"
            dest = folder / "other-out.apkg"
            genanki.Package(deck).write_to_file(str(source), timestamp=1700000000)
            with self.assertRaises(PatchError) as raised:
                patch_apkg(
                    source,
                    dest,
                    css="body{}",
                    afmt="<div></div>\n",
                    model_name=DEFAULT_MODEL,
                )
            self.assertIn("古文草书", str(raised.exception))
            self.assertFalse(dest.exists())
