import json
import os
import shutil
from datetime import date

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "archive")


def save_json(data: dict, path: str) -> None:
    """Write data to path and save a dated copy in archive/."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    basename = os.path.splitext(os.path.basename(path))[0]
    dated    = os.path.join(ARCHIVE_DIR, f"{basename}_{date.today()}.json")
    shutil.copy2(path, dated)


def save_html(html: str, path: str) -> None:
    """Write HTML to path and save a dated copy in archive/."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    with open(path, "w") as f:
        f.write(html)

    basename = os.path.splitext(os.path.basename(path))[0]
    dated    = os.path.join(ARCHIVE_DIR, f"{basename}_{date.today()}.html")
    shutil.copy2(path, dated)
