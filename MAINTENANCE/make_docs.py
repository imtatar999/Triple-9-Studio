"""Writes docs/HOW-TO-USE.md from the program's own Help > How To Use text (studio_help.py), so the GitHub page and
the window always say the same. Run:  python MAINTENANCE/make_docs.py"""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
import studio_help as help_text  # noqa: E402


def markdown(text):
    text = text.replace("\n", "\n")
    out = []
    for line in text.splitlines():
        if line.startswith("# "):
            line = "## " + line[2:]
        elif line.startswith("## "):
            line = "### " + line[3:]
        line = re.sub(r'\*\*"([^"]+)"\*\*', r"**\1**", line)  # **"Button"** -> **Button**
        out.append(line)
    return "\n".join(out)


parts = ["# Triple 9 Studio - how to use\n",
         "This is the same guide as **Help > How To Use** inside the program. Back to the [main page](../README.md).\n"]
toc = []
body = []
for page, content in help_text.PAGES.items():
    title = page.replace("T9 ", "")
    anchor = re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")
    toc.append(f"- [{title}](#{anchor})")
    body.append(f"\n---\n\n# {title}\n")
    body.append(markdown(content))
parts.append("\n".join(toc))
parts.extend(body)
(root / "docs" / "HOW-TO-USE.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
print("docs/HOW-TO-USE.md written")
