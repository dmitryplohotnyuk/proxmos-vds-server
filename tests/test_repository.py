import importlib.util
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repository_check", ROOT / "scripts/check-repository.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_all_local_documentation_links_resolve():
    for path in ROOT.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(ROOT).parts):
            continue
        for href in checker.links(path.read_text()):
            if not href or href.startswith(("/", "#")) or ":" in href:
                continue
            target = href.split("#", 1)[0]
            assert not target or (path.parent / target).exists(), (path, href)


def test_secret_detection_does_not_need_to_echo_values():
    import base64
    import json
    assert checker.secret_types("tskey-" + "auth-" + "x" * 32) == {"tailscale-key"}
    token = base64.b64encode(json.dumps(dict(a="a" * 32, s="s" * 44, t="t" * 36)).encode()).decode()
    assert checker.secret_types(token) == {"cloudflare-connector-token"}
    assert not checker.secret_types("Target: <UUID>.cfargotunnel.com")


def test_source_archive_excludes_ignored_state(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/package-source.sh", repo / "scripts/package-source.sh")
    (repo / ".gitignore").write_text(".env\nsecrets/\n*.db\n*.tar.gz\n")
    (repo / "README.md").write_text("source")
    (repo / ".env").write_text("do not copy")
    (repo / "app.db").write_text("do not copy")
    (repo / "secrets").mkdir()
    (repo / "secrets/key").write_text("do not copy")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    archive = tmp_path / "source.tar.gz"
    subprocess.run(["bash", str(repo / "scripts/package-source.sh"), str(archive)], check=True)
    with tarfile.open(archive) as package:
        assert set(package.getnames()) == {".gitignore", "README.md", "scripts/package-source.sh"}
