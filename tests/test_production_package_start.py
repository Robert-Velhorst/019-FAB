import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["start", "start:fab"])
def test_package_production_start_is_portable_and_sets_environment_before_import(tmp_path, mode):
    pnpm = shutil.which("pnpm.cmd") if os.name == "nt" else shutil.which("pnpm")
    if not pnpm:
        pytest.skip("pnpm unavailable")
    package = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    fixture = tmp_path / "installation with spaces"
    (fixture / "dist").mkdir(parents=True)
    (fixture / "package.json").write_text(
        json.dumps({"private": True, "type": "module", "scripts": package["scripts"]}), encoding="utf-8"
    )
    helper = ROOT / "web/scripts/production-env.mjs"
    if helper.exists():
        (fixture / "scripts").mkdir()
        shutil.copyfile(helper, fixture / "scripts/production-env.mjs")
    for entry in ("index.js", "fab-standalone.js"):
        (fixture / "dist" / entry).write_text(
            "if(process.env.NODE_ENV !== 'production') process.exit(9);\n"
            "console.log(JSON.stringify({environment:process.env.NODE_ENV}));\n", encoding="utf-8"
        )
    env = {**os.environ, "NODE_ENV": "development"}
    result = subprocess.run([pnpm, "--dir", str(fixture), "run", mode],
                            shell=os.name == "nt", capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"environment": "production"}


def test_container_healthcheck_uses_the_secret_file_aware_probe():
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    health = source.split("HEALTHCHECK", 1)[1].split("\nCMD ", 1)[0]
    assert 'CMD ["python", "-m", "src.run_health_probe", "api"]' in health
