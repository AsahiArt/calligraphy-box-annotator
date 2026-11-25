#!/usr/bin/env python3
"""Replace an Anki model's CSS and back template inside an .apkg.

Notes, cards, scheduling, media, deck ids, model ids, and note GUIDs are
left as they are. The front template (``qfmt``) is not rewritten.

The bundled back is the compact header used by the default ``cba`` cards,
written for the existing 书谱 model fields (Simplified, Traditional, Rank,
Same, CursiveChar, ImgSun, ImgMifu, ImgHuaisu). ``Rank`` stays on the note
and is still not shown.

``collection.anki2`` and ``collection.anki21`` are patched directly.
``collection.anki21b`` is patched when the ``zstandard`` module is installed.
A package that only has ``collection.anki21b`` and no zstandard exits with
an error instead of writing a file Anki would ignore.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACK = ROOT / "cba" / "anki_templates" / "shupu_back" / "back.html"
DEFAULT_CSS = ROOT / "cba" / "anki_templates" / "default" / "style.css"
DEFAULT_MODEL = "古文草书·书谱"
_SQLITE_NAMES = ("collection.anki2", "collection.anki21")
_ZSTD_NAME = "collection.anki21b"


class PatchError(Exception):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PatchError(f"cannot read {path.name}: {exc}") from exc


def patch_models(raw: bytes, css: str, afmt: str, model_name: str | None) -> tuple[bytes, list[str]]:
    """Return the updated collection database and the model names that changed."""
    with tempfile.NamedTemporaryFile(suffix=".anki2") as handle:
        handle.write(raw)
        handle.flush()
        conn = sqlite3.connect(handle.name)
        try:
            row = conn.execute("select models from col").fetchone()
            if row is None:
                raise PatchError("collection has no col row")
            models = json.loads(row[0])
            if not isinstance(models, dict):
                raise PatchError("models column is not an object")
            changed: list[str] = []
            now = int(time.time())
            for model in models.values():
                if not isinstance(model, dict):
                    continue
                name = model.get("name")
                if model_name is not None and name != model_name:
                    continue
                previous_id = model.get("id")
                model["css"] = css
                model["mod"] = now
                if model.get("id") != previous_id:
                    raise PatchError("refusing to change a model id")
                templates = model.get("tmpls") or []
                if not isinstance(templates, list) or not templates:
                    raise PatchError(f"model {name!r} has no templates")
                for template in templates:
                    if not isinstance(template, dict):
                        raise PatchError(f"model {name!r} has a template that is not an object")
                    template["afmt"] = afmt
                changed.append(str(name))
            if not changed:
                known = ", ".join(str(model.get("name")) for model in models.values() if isinstance(model, dict))
                raise PatchError(f"model not found: {model_name}. models: {known or '(none)'}")
            conn.execute("update col set models = ?", (json.dumps(models, ensure_ascii=False),))
            conn.commit()
        finally:
            conn.close()
        return Path(handle.name).read_bytes(), changed


def _patch_zstd(raw: bytes, css: str, afmt: str, model_name: str | None) -> tuple[bytes, list[str]]:
    try:
        import zstandard
    except ImportError as exc:
        raise PatchError(
            "collection.anki21b needs the zstandard module (python -m pip install zstandard); "
            "the package was not modified"
        ) from exc
    decompressed = zstandard.ZstdDecompressor().decompress(raw)
    updated, names = patch_models(decompressed, css, afmt, model_name)
    compressed = zstandard.ZstdCompressor().compress(updated)
    return compressed, names


def patch_apkg(
    source: Path,
    dest: Path,
    *,
    css: str,
    afmt: str,
    model_name: str | None,
) -> list[str]:
    if not source.is_file():
        raise PatchError(f"apkg not found: {source.name}")
    if source.resolve() == dest.resolve():
        raise PatchError("refusing to overwrite the input; pass a different --out")
    changed: list[str] = []
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        targets = [name for name in names if name in _SQLITE_NAMES or name == _ZSTD_NAME]
        if not targets:
            raise PatchError("apkg has no collection.anki2, collection.anki21, or collection.anki21b")
        if targets == [_ZSTD_NAME]:
            # Fail before writing when the only collection cannot be decoded.
            _patch_zstd(archive.read(_ZSTD_NAME), css, afmt, model_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w") as out:
                for info in archive.infolist():
                    payload = archive.read(info.filename)
                    if info.filename in _SQLITE_NAMES:
                        payload, changed = patch_models(payload, css, afmt, model_name)
                    elif info.filename == _ZSTD_NAME:
                        payload, changed = _patch_zstd(payload, css, afmt, model_name)
                    out.writestr(info, payload)
            partial.replace(dest)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update only the back template and CSS inside an .apkg.")
    parser.add_argument("--apkg", required=True, help="input .apkg")
    parser.add_argument("--out", required=True, help="output .apkg; the input is not overwritten")
    parser.add_argument("--model-name", default=DEFAULT_MODEL, help=f"model to update (default: {DEFAULT_MODEL})")
    parser.add_argument("--all-models", action="store_true", help="update every model in the collection")
    parser.add_argument("--back", type=Path, default=DEFAULT_BACK, help="replacement back template HTML")
    parser.add_argument("--css", type=Path, default=DEFAULT_CSS, help="replacement card CSS")
    args = parser.parse_args(argv)
    try:
        names = patch_apkg(
            Path(args.apkg),
            Path(args.out),
            css=_read(args.css),
            afmt=_read(args.back).strip() + "\n",
            model_name=None if args.all_models else args.model_name,
        )
    except PatchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("updated model back and css: " + ", ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
