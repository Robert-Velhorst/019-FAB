"""Owned hosted SDK experiment; never import publisher code into this helper."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.request

COMMIT = "15454dbe49d827c6081f3d0231e2e7985e517295"
ARCHIVE_SHA512 = "60262ac532c8076382a9feb0f7f55e7f5ddb68155bc1020f71b4f8639e807cee81f5a8d9843843f0992f7a0fe126f4ff8ae325a0a0ff218624c8c30d86becaaa"
OWNER = Path(__file__).resolve().parents[2]
SDK = OWNER / "vendor-drizzle"
OUT = OWNER / "drizzle-sdk-evidence"
assert os.environ.get("GITHUB_REPOSITORY") == "Robert-Velhorst/019-FAB"
assert os.environ.get("GITHUB_ACTIONS") == "true", "Hosted workers only"
OUT.mkdir(exist_ok=True)


def save(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + "\n")


def tracked():
    names = subprocess.check_output(["git", "-C", str(SDK), "ls-files", "-z"]).split(b"\0")
    return {n.decode(): hashlib.sha256((SDK / n.decode()).read_bytes()).hexdigest()
            for n in names if n and (SDK / n.decode()).is_file()}


def prepare(profile):
    assert subprocess.check_output(["git", "-C", str(SDK), "rev-parse", "HEAD"], text=True).strip() == COMMIT
    original = tracked()
    assert len(original) == 1391
    root = json.loads((SDK / "package.json").read_bytes())
    kit = json.loads((SDK / "drizzle-kit/package.json").read_bytes())
    with urllib.request.urlopen("https://registry.npmjs.org/drizzle-kit/-/drizzle-kit-0.31.11.tgz", timeout=60) as response:
        archive = response.read(4 * 1024 * 1024 + 1)
    assert hashlib.sha512(archive).hexdigest() == ARCHIVE_SHA512
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        assert json.load(tf.extractfile("package/package.json")) == kit
    assert kit["version"] == "0.31.11"
    root_path = SDK / "package.json"
    raw = root_path.read_text()
    assert root["packageManager"] == "pnpm@10.6.3"
    root_path.write_text(raw.replace('"pnpm@10.6.3"', '"pnpm@12.16.1"'))
    if profile == "candidate":
        path = SDK / "drizzle-kit/package.json"
        raw = path.read_text()
        for name, prior, target in [("@drizzle-team/brocli", "^0.10.2", "^0.12.1"), ("esbuild", "^0.25.4", "^0.28.2")]:
            old = json.dumps(name) + ": " + json.dumps(prior)
            new = json.dumps(name) + ": " + json.dumps(target)
            assert raw.count(old) == 1
            raw = raw.replace(old, new)
        path.write_text(raw)
    save("original-source-hashes.json", original)
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    pnpm = subprocess.check_output(["pnpm", "--version"], cwd=SDK, text=True).strip()
    assert node == "v26.12.1" and pnpm == "12.16.1"
    save("input.json", {"owner_head": os.environ["GITHUB_SHA"], "publisher_commit": COMMIT,
         "profile": profile, "SDK_version": kit["version"], "original_SDK_scripts": kit["scripts"],
         "actual_node": node, "actual_pnpm": pnpm,
         "root_package_manager_profile_change": "10.6.3 to12.16.1, both comparison profiles",
         "candidate_changes": "Only Brocli and esbuild dependency declarations; every dependency retained",
         "original_SDK_files": 169, "original_test_directory_files": 75, "full_acceptance": False})


def run(label):
    commands = {
        "resolve": (SDK, "pnpm install --lockfile-only --ignore-scripts --no-frozen-lockfile --strict-peer-dependencies\n"),
        "install": (SDK, "pnpm install\n"),
        "prisma": (SDK / "drizzle-orm", "pnpm prisma generate --schema src/prisma/schema.prisma\n"),
        "build": (SDK, "pnpm build\n"),
        "test": (SDK / "integration-tests", "(cd .. && pnpm test --filter drizzle-kit)\n"),
        "raw-audit": (SDK, "pnpm audit --json\n"),
    }
    cwd, command = commands[label]
    if label in {"install", "prisma", "build", "test"}:
        workflow = (SDK / ".github/workflows/release-latest.yaml").read_text()
        expected = command.strip().replace("--filter drizzle-kit", "--filter ${{ matrix.shard }}")
        assert expected in workflow
    assert subprocess.check_output(["pnpm", "--version"], cwd=SDK, text=True).strip() == "12.16.1"
    with (OUT / (label + ".log")).open("wb") as log:
        result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
                                cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=False)
    record = {"command": command, "exit_code": result.returncode,
              "original_publisher_command": label not in {"resolve", "raw-audit"}}
    if label == "resolve" and (SDK / "pnpm-lock.yaml").exists():
        lock = (SDK / "pnpm-lock.yaml").read_bytes()
        (OUT / "resolved-pnpm-lock.yaml").write_bytes(lock)
        record["resolved_lock_sha256"] = hashlib.sha256(lock).hexdigest()
    save(label + "-result.json", record)
    assert result.returncode == 0, label + " failed; unmodified output retained"


def policy():
    lock = (SDK / "pnpm-lock.yaml").read_text()
    prereleases, unknown = [], []
    for document in re.split(r"(?m)^---\n", lock):
        if not document.strip():
            continue
        section = document.split("\npackages:\n", 1)[1].split("\nsnapshots:\n", 1)[0]
        for key in re.findall(r"^  (\S[^\n]*):$", section, re.M):
            key = key.strip("'\"")
            if "@file:" in key or "@link:" in key:
                continue
            match = re.fullmatch(r"(.+)@(\d+\.\d+\.\d+(?:-[^()+]+)?(?:\+[^()]+)?)(?:\([^\n]*\))?", key)
            if not match:
                unknown.append(key)
            elif "-" in match[2] or "+" in match[2]:
                prereleases.append(key)
    save("preinstall-version-policy.json", {"prerelease_or_local_rows": prereleases,
         "unclassified_package_rows": unknown, "exceptions": [], "passed": not prereleases and not unknown})
    assert not prereleases and not unknown, "Complete version policy failed before any original installer"


def audit():
    original = json.loads((OUT / "original-source-hashes.json").read_bytes())
    current = tracked()
    changed = [name for name in original if original[name] != current.get(name)]
    allowed = {"package.json", "drizzle-kit/package.json", "pnpm-lock.yaml"}
    save("source-preservation.json", {"original_payloads": len(original), "changed": changed,
         "all_original_cases_helpers_fixtures_and_commands_unchanged": set(changed) <= allowed,
         "full_SDK_or_application_adoption": False})
    assert original.keys() == current.keys() and set(changed) <= allowed


def restore():
    source = OWNER / "drizzle-lock-input"
    original_input = json.loads((source / "input.json").read_bytes())
    current_input = json.loads((OUT / "input.json").read_bytes())
    assert original_input == current_input
    assert json.loads((source / "original-source-hashes.json").read_bytes()) == json.loads((OUT / "original-source-hashes.json").read_bytes())
    assert json.loads((source / "preinstall-version-policy.json").read_bytes())["passed"]
    result = json.loads((source / "resolve-result.json").read_bytes())
    lock = (source / "resolved-pnpm-lock.yaml").read_bytes()
    assert result["exit_code"] == 0 and hashlib.sha256(lock).hexdigest() == result["resolved_lock_sha256"]
    (SDK / "pnpm-lock.yaml").write_bytes(lock)
    save("restored-normal-lock.json", result)


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["prepare", "resolve", "policy", "restore", "install", "prisma", "build", "test", "raw-audit", "audit"])
parser.add_argument("--profile", choices=["baseline", "candidate"], default="candidate")
args = parser.parse_args()
if args.mode == "prepare":
    prepare(args.profile)
elif args.mode == "policy":
    policy()
elif args.mode == "audit":
    audit()
elif args.mode == "restore":
    restore()
else:
    run(args.mode)
