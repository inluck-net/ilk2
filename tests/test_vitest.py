"""S20 and S21 for vitest (docs/02 §2.11.4), against a real vitest (cached under ~/.cache)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PY_BIN

from ilk2 import batches, gates, plans
from ilk2.gates.env import pinned_env
from ilk2.plans import Gate

# vitest 3.2.4 and 4.1.11 (kira-cloudflare's version on Oct 13): the reporter API changed in 4
# (onFinished -> onTestRunEnd), and a gate that writes no report is unmeasured on every run.
CACHES = {"3": Path.home() / ".cache" / "ilk2-test-vitest" / "node_modules",
          "4": Path.home() / ".cache" / "ilk2-test-vitest4" / "node_modules"}
NODE = shutil.which("node")
NODE = os.path.realpath(NODE) if NODE else None  # fnm's multishell path is per-shell
# The cache is built from tests/fixtures/vitest-cache/package.json (CI does it). Locally a
# missing cache skips these tests; with ILK2_REQUIRE_VITEST=1 (CI) it fails instead, so S20
# and S21 cannot vanish from a green run (Codex stall-suite review).
HAVE = all((c / ".bin" / "vitest").exists() for c in CACHES.values()) and bool(NODE)
REQUIRED = os.environ.get("ILK2_REQUIRE_VITEST") == "1"
pytestmark = pytest.mark.skipif(not HAVE and not REQUIRED, reason="vitest cache or node missing")
PRELUDE = [str(Path(NODE).parent) if NODE else "", PY_BIN, "/usr/bin", "/bin"]

PKG = {"name": "fx", "private": True, "type": "module",
       "scripts": {"test:unit": "vitest run -c vitest.config.ts", "lint": "eslint ."}}
CONFIG = 'export default { test: { include: ["t/**/*.test.ts"], testTimeout: 200 } }\n'
SUM = "export const add = (a: number, b: number) => { throw new Error('todo') }\n"
TEST = ("import { it, expect } from 'vitest'\nimport { add } from '../src/sum'\n"
        "it('adds', () => { expect(add(2, 3)).toBe(5) })\n")


@pytest.fixture(params=sorted(CACHES), ids=lambda v: f"vitest{v}")
def cache(request) -> Path:
    return CACHES[request.param]


def fixture_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    files = {"package.json": json.dumps(PKG), "vitest.config.ts": CONFIG, "src/sum.ts": SUM,
             "t/sum.test.ts": TEST, ".gitignore": "node_modules\n"}
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    g = ["git", "-C", str(root)]
    subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "s"], check=True)
    return root


def test_s21_vitest_timeout_class_rerun_alone(tmp_path, cache):
    root = fixture_repo(tmp_path / "r")
    (root / "node_modules").symlink_to(cache)
    (root / "t" / "slow.test.ts").write_text(
        "import { it } from 'vitest'\nimport { existsSync, writeFileSync } from 'node:fs'\n"
        "import { tmpdir } from 'node:os'\nimport { join } from 'node:path'\n"
        "it('slow once', async () => {\n  const m = join(tmpdir(), 'seen')\n"
        "  if (!existsSync(m)) { writeFileSync(m, 'x'); await new Promise(r => setTimeout(r, 1000)) }\n})\n")
    env = pinned_env(home=tmp_path / "home", tmp=tmp_path / "tmp", path_prelude=PRELUDE)
    g = Gate(name="unit", adapter="vitest", command=["vitest", "run", "-c", "vitest.config.ts", "t/slow.test.ts"],
             cap_s=120)
    r = gates.measure(g, root, env, tmp_path / "work")
    assert r.outcome == "pass" and r.failures == {}, r.tail
    assert (tmp_path / "tmp" / "seen").exists()  # it did time out once, and was re-run alone


def test_vitest_ids_and_fingerprints(tmp_path, cache):
    root = fixture_repo(tmp_path / "r")
    (root / "node_modules").symlink_to(cache)
    env = pinned_env(home=tmp_path / "home", tmp=tmp_path / "tmp", path_prelude=PRELUDE)
    g = Gate(name="unit", adapter="vitest", command=["vitest", "run", "-c", "vitest.config.ts"], cap_s=120)
    r = gates.measure(g, root, env, tmp_path / "work")
    assert r.outcome == "fail" and list(r.failures) == ["t/sum.test.ts::adds"], r.tail
    assert "todo" in r.failures["t/sum.test.ts::adds"].message


def test_s20_package_script_resolves_and_config_edit_changes_version(ilk, tmp_path, monkeypatch, cache):
    monkeypatch.setenv("ILK2_NOTIFY", "log")
    ilk.repo = fixture_repo(ilk.repo)
    ilk.project(path_prelude=PRELUDE,
                setup={"command": ["ln", "-sfn", str(cache), "node_modules"], "cap_s": 60})
    # a shell test gate is rejected at submit
    lint = {"plans": [{"slug": "p", "steps": [{"instructions": "x",
                                               "gates": [{"name": "lint", "command": "bun run lint"}]}]}]}
    with pytest.raises(batches.SubmitRejected):
        ilk.submit(lint)
    # the vitest script resolves to the vitest adapter with its entry recorded
    impl = {"write": "src/sum.ts", "text": "export const add = (a: number, b: number) => a + b\n"}
    cfg_edit = {"write": "vitest.config.ts",
                "text": 'export default { test: { include: ["t/**/*.test.ts"], testTimeout: 300 } }\n'}
    ilk.script([impl, cfg_edit])
    spec = {"plans": [{"slug": "p", "steps": [
        {"title": "impl", "instructions": "implement add", "must_pass": ["t/sum.test.ts::adds"],
         "gates": [{"name": "unit", "command": "bun run test:unit"}]}]}]}
    bid = ilk.submit(spec)
    g = json.loads(ilk.s.one("SELECT gates FROM steps WHERE batch_id = ?", bid)["gates"])[0]
    assert g["adapter"] == "vitest" and g["command"] == ["vitest", "run", "-c", "vitest.config.ts"]
    assert g["script"] == "test:unit" and g["script_entry"] == PKG["scripts"]["test:unit"]
    assert ilk.drive_inprocess(bid) == "shipped"
    gr = ilk.s.one("SELECT * FROM gate_results ORDER BY id DESC LIMIT 1")
    # the base measured before the edit: since dcaac7a a changed config adds a second base row,
    # measured under the step's config, and an unordered read returned either (Oct 22)
    base = ilk.s.one("SELECT spec_version FROM base_measurements WHERE batch_id = ? ORDER BY at LIMIT 1", bid)
    assert gr["spec_version"] != base["spec_version"]  # the config edit changed the gate version
    assert json.loads(gr["details"])["comparable_base"] is False


def test_vitest_prerequisites_present():
    assert HAVE, f"node ({NODE}) or a vitest cache ({CACHES}) is missing"
