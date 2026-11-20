"""Update existing Kodi sidecar .nfo files with refreshed ratings and IDs."""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET


def nfo_candidates(mediatype: str, file_path: str) -> list[str]:
    """Possible .nfo paths for a Kodi library item's ``file`` field."""
    raw = str(file_path or "").strip()
    if not raw:
        return []
    is_url = "://" in raw
    use_posix = is_url or ("/" in raw and "\\" not in raw)
    sep = "/" if use_posix else os.sep
    if mediatype == "tvshow":
        base = raw.rstrip("/\\")
        return [f"{base}{sep}tvshow.nfo"]
    if use_posix:
        stem, _ext = posixpath.splitext(raw)
        folder = posixpath.dirname(raw)
    else:
        stem, _ext = os.path.splitext(raw)
        folder = os.path.dirname(raw)
    candidates = [f"{stem}.nfo"]
    if mediatype == "movie" and folder:
        candidates.append(f"{folder.rstrip('/\\')}{sep}movie.nfo")
    return candidates


def update_nfo_xml(xml_text: str, ratings: dict, uniqueids: dict | None = None) -> str | None:
    """Update <ratings> and missing <uniqueid> elements in an existing NFO XML string."""
    if not xml_text or (not ratings and not uniqueids):
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    changed = False
    if ratings:
        ratings_el = root.find("ratings")
        if ratings_el is None:
            ratings_el = ET.SubElement(root, "ratings")
            changed = True
        has_new_default = any(bool(v.get("default")) for v in ratings.values() if isinstance(v, dict))
        existing_by_name: dict[str, ET.Element] = {}
        for child in list(ratings_el):
            if child.tag != "rating":
                continue
            name = (child.get("name") or "").strip()
            if name:
                existing_by_name[name] = child
            if has_new_default and "default" in child.attrib and name not in ratings:
                del child.attrib["default"]
                changed = True

        for name, data in ratings.items():
            if not isinstance(data, dict):
                continue
            val = float(data.get("rating") or 0)
            if val <= 0 or val > 10:
                continue
            votes = max(0, int(data.get("votes") or 0))
            is_default = bool(data.get("default"))
            val_text = f"{val:.3f}".rstrip("0").rstrip(".")
            if "." not in val_text:
                val_text += ".0"

            el = existing_by_name.get(name)
            if el is None:
                el = ET.SubElement(ratings_el, "rating", {"name": str(name), "max": "10"})
                changed = True
            if is_default:
                if el.get("default") != "true":
                    el.set("default", "true")
                    changed = True
            elif has_new_default and "default" in el.attrib:
                del el.attrib["default"]
                changed = True

            val_el = el.find("value")
            if val_el is None:
                val_el = ET.SubElement(el, "value")
                changed = True
            if (val_el.text or "").strip() != val_text:
                val_el.text = val_text
                changed = True

            votes_el = el.find("votes")
            if votes_el is None:
                votes_el = ET.SubElement(el, "votes")
                changed = True
            if (votes_el.text or "").strip() != str(votes):
                votes_el.text = str(votes)
                changed = True

    if uniqueids:
        existing_types = {
            (el.get("type") or "").strip().lower()
            for el in root.findall("uniqueid")
            if (el.text or "").strip()
        }
        for uid_type, uid_val in uniqueids.items():
            key = str(uid_type).strip()
            val = str(uid_val or "").strip()
            if not key or not val or key.lower() in existing_types:
                continue
            el = ET.SubElement(root, "uniqueid", {"type": key})
            el.text = val
            existing_types.add(key.lower())
            changed = True

    if not changed:
        return None
    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
