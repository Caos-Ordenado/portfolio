"""Exercise the private snapshot contract using disposable local Git repositories."""

import importlib.util
import pathlib
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name.replace('_', '-')}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load("create_snapshot")
verifier = load("verify_snapshot")


def run(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)


@pytest.fixture
def checkout(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "infra"
    repo.mkdir()
    run(repo, "init", "-b", "main")
    for name, content in {
        "hosting/setup.sh": "#!/bin/sh\nexit 0\n",
        "hosting/README.md": "-----BEGIN PRIVATE KEY-----\nexample only\n",
        "hosting/foo*.yaml": "ignored wildcard name\n",
        "hosting/fooX.yaml": "safe: true\n",
        "hosting/.env": "FORBIDDEN=1\n",
        "k8s/example.yaml": "kind: ConfigMap\n",
    }.items():
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    run(repo, "add", ".")
    run(repo, "-c", "user.name=Snapshot Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    real_git = exporter.git

    def local_git(repo: pathlib.Path, *args: str) -> bytes:
        if args[0] == "ls-remote":
            return real_git(repo, "rev-parse", "HEAD").strip() + b"\trefs/heads/main\n"
        return real_git(repo, *args)

    monkeypatch.setattr(exporter, "git", local_git)
    output = tmp_path / "private"
    output.mkdir(mode=0o700)
    return repo, output


def test_curated_archive_and_blob_verification(checkout, tmp_path: pathlib.Path):
    repo, output = checkout
    sha, count, _digest, snapshot = exporter.export(repo, output)
    assert sha == exporter.git(repo, "rev-parse", "HEAD").decode().strip()
    assert count == 3
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(["tar", "-xf", str(snapshot / "snapshot.tar"), "-C", str(extracted)], check=True)
    verifier.verify(extracted, snapshot / "ls-tree.bin")
    assert not (extracted / "hosting/README.md").exists()
    assert not (extracted / "hosting/.env").exists()
    assert not (extracted / "hosting/foo*.yaml").exists()

    (extracted / "hosting/fooX.yaml").write_text("changed: true\n")
    with pytest.raises(ValueError, match="blob mismatch"):
        verifier.verify(extracted, snapshot / "ls-tree.bin")


def test_rejects_changed_checkout_and_embedded_token(checkout):
    repo, output = checkout
    (repo / "hosting/setup.sh").write_text("different\n")
    with pytest.raises(ValueError, match="checkout has changes"):
        exporter.export(repo, output)
    run(repo, "checkout", "--", "hosting/setup.sh")
    (repo / "hosting/setup.sh").write_text("ghp_" + "A" * 30 + "\n")
    run(repo, "add", ".")
    run(repo, "-c", "user.name=Snapshot Test", "-c", "user.email=test@example.invalid", "commit", "-m", "unsafe")
    with pytest.raises(ValueError, match="unsafe tracked content"):
        exporter.export(repo, output)


def test_rejects_link_and_bad_manifest(checkout, tmp_path: pathlib.Path):
    repo, output = checkout
    (repo / "hosting/link.yaml").symlink_to("fooX.yaml")
    run(repo, "add", ".")
    run(repo, "-c", "user.name=Snapshot Test", "-c", "user.email=test@example.invalid", "commit", "-m", "link")
    with pytest.raises(ValueError, match="symlink"):
        exporter.export(repo, output)
    bad_manifest = tmp_path / "ls-tree.bin"
    bad_manifest.write_bytes(b"100644 blob " + b"0" * 40 + b"\t../escape\0")
    with pytest.raises(ValueError, match="unsafe tracked path"):
        verifier.verify(repo, bad_manifest)


def test_rejects_extracted_mode_mismatch(checkout, tmp_path: pathlib.Path):
    repo, output = checkout
    _sha, _count, _digest, snapshot = exporter.export(repo, output)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(["tar", "-xf", str(snapshot / "snapshot.tar"), "-C", str(extracted)], check=True)
    (extracted / "hosting/fooX.yaml").chmod(0o755)
    with pytest.raises(ValueError, match="file mode mismatch"):
        verifier.verify(extracted, snapshot / "ls-tree.bin")
