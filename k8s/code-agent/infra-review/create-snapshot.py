#!/usr/bin/env python3
"""Export an audited, tracked-only subset of private infra for the isolated terminal."""

import hashlib
import os
import pathlib
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ALLOWED_PREFIXES = ("hosting/", "k8s/", "services/web/", "services/web_espacioraizconsciente/")
TEXT_SUFFIXES = {".sh", ".md", ".yaml", ".yml", ".vue", ".ts", ".js", ".css", ".scss", ".svg", ".json"}
DENIED_PARTS = {".github", ".git", "certs", "secrets", "credentials", "generated", "node_modules", ".nuxt", ".output"}
DENIED_NAMES = {"agents.md", "dockerfile", "deploy.sh", "deploy-hetzner.sh"}
# This operator runbook contains a PEM-shaped example; keep it off the terminal.
DENIED_EXACT = {"hosting/README.md"}
SENSITIVE = re.compile(r"(?i)(secret|credential|password|private[_-]?key|token|\.env|\.pem|\.key|\.p12|\.pfx)")
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----")
TOKEN = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")


def git(repo: pathlib.Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def include(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        path.isascii()
        and not any(ch in path for ch in "*?[]:\\")
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
        and path not in DENIED_EXACT
        and path.startswith(ALLOWED_PREFIXES)
        and all(part and part not in (".", "..") and part.casefold() not in DENIED_PARTS
                and not part.startswith(".") and not SENSITIVE.search(part) for part in parts)
        and name.casefold() not in DENIED_NAMES
        and not name.casefold().startswith("dockerfile")
        and pathlib.PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES
    )


def export(repo: pathlib.Path, output_dir: pathlib.Path) -> tuple[str, int, str, pathlib.Path]:
    if not repo.is_dir() or not output_dir.is_dir() or stat.S_IMODE(output_dir.stat().st_mode) != 0o700:
        raise ValueError("repo or private 0700 output directory unavailable")
    if git(repo, "branch", "--show-current").strip() != b"main":
        raise ValueError("infra checkout must be on main")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ValueError("infra checkout has changes")
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    remote = git(repo, "ls-remote", "origin", "refs/heads/main").decode().split()[0]
    if head != remote or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("infra checkout does not match remote main")

    entries: list[bytes] = []
    paths: list[str] = []
    for record in git(repo, "ls-tree", "-rz", "--full-tree", head).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, _blob = metadata.split(b" ")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("tracked symlink/submodule/special file; abort")
        path = raw_path.decode("utf-8")
        if include(path):
            content = git(repo, "show", f"{head}:{path}")
            if (b"\x00" in content or PRIVATE_KEY.search(content) or TOKEN.search(content)
                    or any(byte < 32 and byte not in (9, 10, 13) for byte in content)):
                raise ValueError("unsafe tracked content; abort")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("non-text tracked content; abort") from None
            entries.append(record + b"\0")
            paths.append(path)
    if not paths or git(repo, "rev-parse", "HEAD").decode().strip() != head or git(repo, "status", "--porcelain=v1").strip():
        raise ValueError("empty or changed infra checkout")

    previous_umask = os.umask(0o077)
    try:
        snapshot_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"infra-{head[:12]}-", dir=output_dir))
        archive_name = snapshot_dir / "snapshot.tar"
        manifest_name = snapshot_dir / "ls-tree.bin"
        try:
            with archive_name.open("xb") as archive:
                subprocess.run(["git", "-C", str(repo), "--literal-pathspecs", "archive", "--format=tar", head, "--", *paths],
                               stdout=archive, stderr=subprocess.DEVNULL, check=True)
            with manifest_name.open("xb") as manifest:
                manifest.write(b"".join(entries))
            found: set[str] = set()
            with tarfile.open(archive_name, "r:") as archive:
                for member in archive:
                    name = member.name
                    parts = pathlib.PurePosixPath(name).parts
                    if not parts or name.startswith("/") or ".." in parts:
                        raise ValueError("unsafe archive member")
                    if member.isfile():
                        if name in found:
                            raise ValueError("duplicate archive member")
                        found.add(name)
                    elif not member.isdir():
                        raise ValueError("non-regular archive member")
            if found != set(paths):
                raise ValueError("archive differs from audited paths")
            digest = hashlib.sha256(archive_name.read_bytes()).hexdigest()
        except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError):
            archive_name.unlink(missing_ok=True)
            manifest_name.unlink(missing_ok=True)
            snapshot_dir.rmdir()
            raise
    finally:
        os.umask(previous_umask)
    return head, len(paths), digest, snapshot_dir


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: create-snapshot.py INFRA_REPO PRIVATE_0700_OUTPUT_DIR")
    sha, count, digest, directory = export(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
    print(f"main_sha={sha} tracked_files={count} archive_sha256={digest} snapshot_dir={directory}")
