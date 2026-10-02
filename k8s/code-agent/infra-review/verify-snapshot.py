#!/usr/bin/env python3
"""Compare a fresh extracted archive against a NUL-delimited git ls-tree manifest."""

import hashlib
import os
import pathlib
import stat
import sys


def verify(root: pathlib.Path, manifest: pathlib.Path) -> None:
    expected = {}
    for record in manifest.read_bytes().split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, blob = metadata.split(b" ")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("snapshot contains a symlink, submodule or unsupported entry")
        name = os.fsdecode(raw_path)
        parts = pathlib.PurePosixPath(name).parts
        if not parts or any(p in ("..", ".") for p in parts) or name.startswith("/"):
            raise ValueError("unsafe tracked path")
        if name in expected:
            raise ValueError("duplicate tracked path")
        expected[name] = (mode, blob.decode("ascii"))

    if not expected or not root.is_dir() or root.is_symlink():
        raise ValueError("missing or unsafe snapshot")

    found = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = pathlib.Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("link in extracted tree")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("special file in extracted tree")
            if stat.S_ISREG(info.st_mode):
                relative = path.relative_to(root).as_posix()
                if relative not in expected:
                    raise ValueError("untracked file in extracted tree")
                mode, blob = expected[relative]
                actual_mode = b"100755" if info.st_mode & 0o111 else b"100644"
                if actual_mode != mode:
                    raise ValueError("file mode mismatch")
                data = path.read_bytes()
                digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                if digest != blob:
                    raise ValueError("git blob mismatch")
                found.add(relative)
    if found != expected.keys():
        raise ValueError("missing tracked files")
    print(f"Verified {len(found)} tracked files against git blob IDs (no links)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: verify-snapshot.py EXTRACTED_ROOT LS_TREE_MANIFEST")
    verify(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
