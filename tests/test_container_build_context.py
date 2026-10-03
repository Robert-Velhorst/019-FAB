import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def local_docker_command(docker, context, *args):
    return [docker, "--context", context, *args]


def is_local_builder(output, context):
    fields = {}
    for line in output.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if separator and key in {"Name", "Driver"}:
            if key in fields:
                return False
            fields[key] = value.strip()
    return fields == {"Name": context, "Driver": "docker"}


@pytest.mark.parametrize("output,expected", [
    ("Name: desktop-linux\nDriver: docker\n\nNodes:\nName: desktop-linux", True),
    ("Name: default\nDriver: docker\n", False),
    ("Name: desktop-linux\nDriver: docker-container\n", False),
    ("Name: desktop-linux\nDriver: cloud\n", False),
    ("Name: desktop-linux\nDriver: docker\nDriver: cloud\n", False),
])
def test_targeted_builder_inspection_rejects_foreign_or_ambiguous_drivers(output, expected):
    assert is_local_builder(output, "desktop-linux") is expected


def test_build_context_commands_pin_the_verified_engine():
    assert local_docker_command("docker", "desktop-linux", "info") == [
        "docker", "--context", "desktop-linux", "info",
    ]


@pytest.mark.parametrize("context", ["python", "web"])
def test_real_docker_context_excludes_runtime_data_and_keeps_build_sources(tmp_path, context):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker unavailable")
    selected = subprocess.run([docker, "context", "show"], capture_output=True, text=True, timeout=30)
    assert selected.returncode == 0
    builder = selected.stdout.strip()
    assert builder, "Cannot identify Docker context"
    endpoint = subprocess.run(local_docker_command(docker, builder, "context", "inspect", builder,
                                                  "--format", "{{.Endpoints.docker.Host}}"),
                              capture_output=True, text=True, timeout=30)
    assert endpoint.returncode == 0, "Cannot verify Docker endpoint"
    host = endpoint.stdout.strip()
    if not (host.startswith("npipe:") or host.startswith("unix:")):
        pytest.skip("Build-context fixtures require a local Docker engine")
    buildx = subprocess.run(local_docker_command(docker, builder, "buildx", "version"),
                            capture_output=True, text=True, timeout=30)
    if buildx.returncode:
        pytest.skip("Docker Buildx is unavailable")
    try:
        info = subprocess.run(local_docker_command(docker, builder, "info", "--format", "{{.ServerVersion}}"),
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.skip("Local Docker engine did not answer its read-only health query")
    if info.returncode:
        pytest.skip("Local Docker engine unavailable")
    listed = subprocess.run(local_docker_command(docker, builder, "buildx", "inspect", builder),
                            capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0
    assert is_local_builder(listed.stdout, builder), (
        "Fixtures require the context-associated local Docker driver"
    )
    directory = tmp_path / "context"
    directory.mkdir()
    ignore = ROOT / ("web/.dockerignore" if context == "web" else ".dockerignore")
    shutil.copyfile(ignore, directory / ".dockerignore")
    (directory / "Dockerfile").write_text("FROM scratch\nCOPY . /context\n", encoding="utf-8")
    kept = (["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "vite.config.ts", "tsconfig.json",
             "client/src/App.tsx", "client/public/fab-mark.svg", "server/fabStandalone.ts", "shared/types.ts",
             "drizzle/schema.ts", "scripts/production-env.mjs", "patches/wouter.patch"] if context == "web" else
            ["requirements.txt", "src/main.py", "src/operations/local_api.py", "config/config_template.ini"])
    excluded = [".env", ".env.production", "config/config.ini", "credentials/provider.json",
                "tokens/google.json", "data/ledger.sqlite3", "output/receipt.txt", "backups/private.bin",
                "downloads/receipt.jpg", ".git/config", "node_modules/stale.bin", "dist/old.js",
                "dist-bundle-test/old.js", "private-unlisted-file.txt", "config/extra-secret.txt",
                "src/__pycache__/main.pyc", "server/.env.production", "server/output/receipt.txt"]
    for name in kept + excluded:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture only", encoding="utf-8")
    identity = uuid.uuid4().hex
    image = f"fab-context-test:{identity}"
    container = f"fab-context-test-{identity}"

    def run(*args, timeout=30):
        return subprocess.run(local_docker_command(docker, builder, *args), capture_output=True, text=True, timeout=timeout)

    try:
        built = run("build", "--builder", builder, "--network", "none", "--tag", image, str(directory), timeout=90)
        assert built.returncode == 0, built.stdout + built.stderr
        created = run("create", "--name", container, image, "/never-executed")
        assert created.returncode == 0, created.stderr
        copied = run("cp", f"{container}:/context", str(tmp_path / "extracted"))
        assert copied.returncode == 0, copied.stderr
        for name in kept:
            assert (tmp_path / "extracted" / name).is_file(), f"Missing build source: {name}"
        for name in excluded:
            assert not (tmp_path / "extracted" / name).exists(), f"Runtime/private file entered image: {name}"
    finally:
        run("rm", "--force", container)
        run("image", "rm", "--force", image)
