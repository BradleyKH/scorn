"""Layout hints in the exported MusicXML."""

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

BREAK = re.compile(r'\s+new-(?:page|system)="yes"')


def strip_breaks(path: Path) -> None:
    """Drop the source's page and system breaks so notation programs lay the score out themselves."""
    if path.suffix.lower() != ".mxl":
        path.write_text(BREAK.sub("", path.read_text(encoding="utf-8")), encoding="utf-8", newline="")
        return
    with zipfile.ZipFile(path) as z:
        entries = [(info, z.read(info)) for info in z.infolist()]
        container = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = container.find(".//rootfile").get("full-path")
    with zipfile.ZipFile(path, "w") as z:
        for info, data in entries:
            if info.filename == rootfile:
                data = BREAK.sub("", data.decode("utf-8")).encode("utf-8")
            z.writestr(info, data)
