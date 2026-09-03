#!/usr/bin/env python3
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

def infer(text):
    m = re.search(r"\[([^\[\]]+?)\s+(\d+(?:\.\d+)?)\]", text or "")
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2))

def repair(p, fallback=""):
    if p.suffix.lower() != ".epub":
        return

    guessed = infer(p.name) or infer(fallback)
    if not guessed:
        print(f"SERIESFIX skip: no confident series in {p.name}", file=sys.stderr)
        return

    series, seq = guessed

    with zipfile.ZipFile(p, "r") as zin:
        infos = zin.infolist()
        opf = next((i.filename for i in infos if i.filename.lower().endswith(".opf")), None)
        if not opf:
            return

        root = ET.fromstring(zin.read(opf))
        metadata = next(
            (e for e in root.iter() if e.tag.split("}")[-1] == "metadata"),
            None,
        )
        if metadata is None:
            return

        existing = {}
        for e in metadata:
            if e.tag.split("}")[-1] == "meta":
                name = e.attrib.get("name", "")
                if name in ("calibre:series", "calibre:series_index"):
                    existing[name] = e.attrib.get("content", "").strip()

        # Never overwrite valid embedded series metadata.
        if existing.get("calibre:series") or existing.get("calibre:series_index"):
            print(f"SERIESFIX keep existing metadata: {p.name}", file=sys.stderr)
            return

        ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
        tag = f"{{{ns}}}meta" if ns else "meta"

        ET.SubElement(
            metadata, tag,
            {"name": "calibre:series", "content": series},
        )
        ET.SubElement(
            metadata, tag,
            {"name": "calibre:series_index", "content": str(seq)},
        )

        new_opf = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        tmp = p.with_suffix(".seriesfix.tmp")

        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                data = new_opf if info.filename == opf else zin.read(info.filename)
                zout.writestr(info, data)

    shutil.copystat(p, tmp)
    tmp.replace(p)
    print(f"SERIESFIX patched: {p.name} -> {series} #{seq:g}", file=sys.stderr)

try:
    target = Path(sys.argv[1])

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    fallback = ((payload.get("task") or {}).get("title") or "")

    if target.is_file():
        repair(target, fallback)
    elif target.is_dir():
        for p in target.rglob("*.epub"):
            repair(p, fallback)

except Exception as e:
    # Metadata repair must never make an otherwise successful download fail.
    print(f"SERIESFIX warning: {e}", file=sys.stderr)
