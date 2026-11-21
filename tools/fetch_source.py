"""Build step: unpack sqlglot's Python files at the pinned commit into /app/src.

The selector compiles patched files against them. If the download fails the
build still succeeds and the selector runs every test on Python code changes.
"""

import io
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{PIN}.tar.gz"
TARGET = Path("/app/src")


def main() -> None:
    for attempt in range(5):
        try:
            payload = urllib.request.urlopen(URL, timeout=60).read()
            break
        except OSError as error:
            print(f"download failed: {error}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    else:
        return
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive.getmembers():
            parts = member.name.split("/")[1:]
            if member.isfile() and member.name.endswith(".py") and parts[0] in ("sqlglot", "tests") \
                    and ".." not in parts:
                target = TARGET.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(0o644)
    for folder in [TARGET, *TARGET.rglob("*")]:
        if folder.is_dir():
            folder.chmod(0o755)


if __name__ == "__main__":
    main()
