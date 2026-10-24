#!/usr/bin/env python3
"""Opt-in, pinned Node/SQLite deployment; called under deploy-my-server's lock."""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import signal
import socket
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

from source import read_source

HERE = Path(__file__).resolve().parent
BASE = Path("/opt/dyyjs-devblog")
DATA = Path("/var/lib/dyyjs-devblog")
CONFIG = Path("/etc/dyyjs-devblog")
UNIT = Path("/etc/systemd/system/dyyjs-devblog.service")
SERVICE = UNIT.name
NODE = BASE / "node/bin/node"
PROXY = "http://127.0.0.1:7890"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def root_owned(path, private=False):
    """Reject writable configuration/runtime paths, including their parents."""
    resolved = path.resolve(strict=True)
    for entry in (resolved, *resolved.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"must be root-owned and not group/world writable: {entry}")
    if private and resolved.stat().st_mode & 0o077:
        raise RuntimeError(f"must be readable only by root: {path}")


def validate_env(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1 or not re.fullmatch(
        r"ADMIN_PASSWORD_HASH=(\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53})", lines[0]
    ):
        raise ValueError("env must contain only ADMIN_PASSWORD_HASH=<unquoted bcrypt hash>")


def check_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("database integrity check failed")
        # Refuse to silently replace the existing blog with an empty database.
        db.execute("SELECT id, slug FROM posts LIMIT 0")


def backup_database(source, destination):
    if destination.exists():
        raise RuntimeError("backup destination already exists")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(destination)) as backup:
            live.backup(backup)
    destination.chmod(0o600)
    check_database(destination)


def as_user(user, cwd, *command, variables=None):
    environment = {
        "PATH": f"{NODE.parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/var/cache/dyyjs-devblog-build" if user == "devblog-build" else str(DATA),
        "ASTRO_TELEMETRY_DISABLED": "1",
    }
    environment.update(variables or {})
    # No production credentials, inherited NODE_OPTIONS or shell evaluation.
    run("runuser", "-u", user, "--", "env", "-i",
        *(f"{key}={value}" for key, value in environment.items()), *command, cwd=cwd)


def preflight():
    for path in (CONFIG / "enabled", CONFIG / "env", NODE, BASE, BASE / "releases", BASE / "incoming"):
        root_owned(path, private=path == CONFIG / "env")
    validate_env((CONFIG / "env").read_text())
    for user in ("devblog", "devblog-build"):
        run("id", user, stdout=subprocess.DEVNULL)
    run(NODE, HERE / "check-runtime.mjs")
    if (BASE / "current").exists() and not (BASE / "current").is_symlink():
        raise RuntimeError("current must be a managed symlink")
    if (BASE / "current").is_symlink():
        current = (BASE / "current").resolve(strict=True)
        if current.parent != BASE / "releases":
            raise RuntimeError("current must point directly inside releases")
    if (DATA / "data.db").is_symlink():
        raise RuntimeError("database must be a local regular file")
    active = service_state("is-active") == "active"
    if active and not (BASE / "current").is_symlink():
        raise RuntimeError("refusing to replace an unmanaged running blog service")
    if not active:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 4321))
    import pwd
    owner = pwd.getpwnam("devblog").pw_uid
    for path in (DATA, DATA / "backups", DATA / "data.db"):
        if path.is_symlink() or path.stat().st_uid != owner or path.stat().st_mode & 0o077:
            raise RuntimeError(f"must be private, owned by devblog, and not a symlink: {path}")
    check_database(DATA / "data.db")
    if shutil.disk_usage(BASE).free < 2 * 1024**3:
        raise RuntimeError("at least 2 GiB free is required for build staging")


def freeze_tree(path):
    for root, directories, files in os.walk(path, followlinks=False):
        for entry in [Path(root), *(Path(root) / name for name in directories + files)]:
            if entry.is_symlink():
                if not entry.resolve().is_relative_to(path):
                    raise RuntimeError("release contains an escaping symlink")
                os.chown(entry, 0, 0, follow_symlinks=False)
                continue
            if not (entry.is_file() or entry.is_dir()):
                raise RuntimeError("release contains a special file")
            os.chown(entry, 0, 0)
            entry.chmod(0o755 if entry.is_dir() or entry.stat().st_mode & stat.S_IXUSR else 0o644)


def prepare_release(server_sha, source, operation):
    release = BASE / "releases" / server_sha
    metadata = {"server_commit": server_sha, "upstream_commit": source["commit"],
                "site": source["site"], "base": source["base"]}
    if release.exists():
        root_owned(release)
        if release.is_symlink() or json.loads((release / ".devblog-release.json").read_text()) != metadata:
            raise RuntimeError("existing release does not match the reviewed source")
        if not (release / "dist/server/entry.mjs").is_file():
            raise RuntimeError("existing release is incomplete")
        return release
    if operation == "rollback":
        raise RuntimeError("rollback requires an already verified local blog release")
    stage = Path(tempfile.mkdtemp(prefix="build-", dir=BASE / "incoming"))
    try:
        shutil.chown(stage, user="devblog-build", group="devblog-build")
        build_env = {"HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY,
                     "PUBLIC_RELEASE_ID": source["commit"],
                     "SITE_URL": source["site"], "PUBLIC_BASE_PATH": source["base"],
                     "DATABASE_PATH": str(stage / "verify.db"),
                     "NODE_OPTIONS": "--max-old-space-size=768"}
        as_user("devblog-build", stage, "git", "init", "-q")
        as_user("devblog-build", stage, "git", "-c", f"http.proxy={PROXY}", "fetch", "--depth=1",
                f"https://github.com/{source['repository']}.git", source["commit"])
        as_user("devblog-build", stage, "git", "checkout", "--detach", "FETCH_HEAD")
        # The fetch target is an immutable full SHA, not an upstream branch.
        head = run("git", "-c", f"safe.directory={stage}", "rev-parse", "HEAD", cwd=stage,
                   capture_output=True, text=True).stdout.strip()
        if head != source["commit"]:
            raise RuntimeError("upstream checkout did not match the pin")
        for command in (("ci",), ("run", "verify"), ("audit", "--audit-level=moderate")):
            as_user("devblog-build", stage, "npm", *command, variables=build_env)
        shutil.rmtree(stage / ".git")
        for path in stage.glob("verify.db*"):
            path.unlink()
        (stage / ".devblog-release.json").write_text(json.dumps(metadata) + "\n")
        shutil.copyfile(HERE / "systemd" / SERVICE, stage / SERVICE)
        freeze_tree(stage)
        stage.rename(release)
        return release
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def migrate(release, database, backups):
    as_user("devblog", release, NODE, "scripts/migrate.mjs", variables={
        "DATABASE_PATH": str(database), "BACKUP_DIR": str(backups)})
    check_database(database)


def rehearse(release):
    with tempfile.TemporaryDirectory(prefix="rehearsal-", dir=DATA) as directory:
        staging = Path(directory)
        shutil.chown(staging, user="devblog", group="devblog")
        database = staging / "data.db"
        backup_database(DATA / "data.db", database)
        shutil.chown(database, user="devblog", group="devblog")
        migrate(release, database, staging / "backups")


def service_state(verb):
    result = subprocess.run(["systemctl", verb, SERVICE], capture_output=True, text=True)
    return result.stdout.strip()


def get_current():
    link = BASE / "current"
    return link.resolve() if link.is_symlink() else None


def set_current(target):
    link = BASE / "current"
    if target is None:
        link.unlink(missing_ok=True)
        return
    temporary = BASE / ("current-" + uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def write_unit(content):
    # The outer deployer snapshots units with hardlinks; never truncate that inode.
    if content is None:
        UNIT.unlink(missing_ok=True)
        return
    temporary = UNIT.with_name(SERVICE + "." + uuid.uuid4().hex)
    temporary.write_bytes(content)
    temporary.chmod(0o644)
    os.replace(temporary, UNIT)


def wait_healthy(expected, base="", attempts=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(attempts):
        try:
            with opener.open(f"http://127.0.0.1:4321{base}/api/health", timeout=2) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("status") == "ok" and payload.get("release") == expected:
                    return
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise RuntimeError("blog health did not match the pinned upstream commit")


def activate(release, source):
    previous = get_current()
    old_unit = UNIT.read_bytes() if UNIT.exists() else None
    was_active = service_state("is-active") == "active"
    was_enabled = service_state("is-enabled") == "enabled"
    run("systemd-analyze", "verify", release / SERVICE)
    try:
        if was_active:
            run("systemctl", "stop", SERVICE)
        backup = DATA / "backups" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".db")
        backup_database(DATA / "data.db", backup)
        migrate(release, DATA / "data.db", DATA / "backups")
        write_unit((release / SERVICE).read_bytes())
        set_current(release)
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", SERVICE)
        run("systemctl", "start", SERVICE)
        wait_healthy(source["commit"], source["base"])
        run("systemctl", "enable", SERVICE)
    except BaseException:
        # Restore code/service state only. Never overwrite new writes with an old DB.
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        set_current(previous)
        write_unit(old_unit)
        run("systemctl", "daemon-reload")
        if old_unit is not None:
            run("systemctl", "enable" if was_enabled else "disable", SERVICE)
            if was_active:
                run("systemctl", "reset-failed", SERVICE)
                run("systemctl", "start", SERVICE)
                prior = json.loads((previous / ".devblog-release.json").read_text())
                wait_healthy(prior["upstream_commit"], prior.get("base", ""))
        raise


def deploy(operation, server_sha):
    if operation not in ("deploy", "rollback") or not re.fullmatch(r"[0-9a-f]{40}", server_sha):
        raise ValueError("expected deploy|rollback and a full server-repository SHA")
    preflight()
    source = read_source()
    release = prepare_release(server_sha, source, operation)
    rehearse(release)
    activate(release, source)
    print(f"Dev Blog ready: server={server_sha} upstream={source['commit']}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or os.geteuid() != 0:
        sys.exit("usage (root): deploy.py deploy|rollback <server-repository-sha>")
    # Also serialize manual calls independently of the repository-wide deploy lock.
    import fcntl
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("deployment interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    with open("/run/lock/dyyjs-devblog.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy(*sys.argv[1:])
