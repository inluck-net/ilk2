import os
import subprocess
import sys
from pathlib import Path

import pytest

from ilk2 import gates
from ilk2.gates.env import pinned_env
from ilk2.plans import Gate

PY_BIN = str(Path(sys.executable).parent)


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root


def env_for(tmp_path: Path, **kw) -> dict:
    return pinned_env(home=tmp_path / "home", tmp=tmp_path / "tmp",
                      path_prelude=[PY_BIN, "/usr/bin", "/bin"], **kw)


def gate(*paths, cap=120, declares=True, name="unit"):
    return Gate(name=name, adapter="pytest",
                command=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
                cap_s=cap, declares_tests=declares)


def m(g, root, tmp_path, **kw):
    return gates.measure(g, root, env_for(tmp_path, **kw), tmp_path / "work")


def test_pass_and_fail_with_fingerprints(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "def test_ok():\n    assert 1\n"
                                                  "def test_bad():\n    assert 1 == 2\n"})
    r = m(gate("tests/test_a.py"), root, tmp_path)
    assert r.outcome == "fail" and list(r.failures) == ["tests/test_a.py::test_bad"]
    assert r.collected == 2
    v = gates.judge(r, base=r.failure_map())
    assert v.outcome == "pass"  # same failure at the base is not new


def test_s15_changed_failure_is_new(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "def test_bad():\n    assert 1 == 2\n"})
    base = m(gate("tests/test_a.py"), root, tmp_path).failure_map()
    (root / "tests/test_a.py").write_text("def test_bad():\n    assert 3 == 4\n")
    v = gates.judge(m(gate("tests/test_a.py"), root, tmp_path), base=base)
    assert v.outcome == "fail" and v.new_failures == ["tests/test_a.py::test_bad"]


def test_volatile_values_do_not_change_fingerprint(tmp_path):
    body = ("import uuid, tempfile\ndef test_bad():\n"
            "    assert str(uuid.uuid4()) == tempfile.mkdtemp()\n")
    root = project(tmp_path, {"tests/test_a.py": body})
    a = m(gate("tests/test_a.py"), root, tmp_path).failure_map()
    b = m(gate("tests/test_a.py"), root, tmp_path).failure_map()
    assert a == b


def test_absent_test_file_measures_zero_tests(tmp_path):
    root = project(tmp_path, {"tests/__init__.py": ""})
    r = m(gate("tests/test_new.py"), root, tmp_path)
    assert r.reason == "no-tests" and r.missing_paths == ["tests/test_new.py"]
    assert r.failure_map() == {}


def test_s4_runner_missing_is_permanent_unmeasured(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "def test_ok():\n    pass\n"})
    g = Gate(name="u", adapter="pytest", command=["no-such-python-xyz", "-m", "pytest"], cap_s=30)
    r = m(g, root, tmp_path)
    assert r.outcome == "unmeasured" and not r.transient and "runner-not-found" in r.reason


def test_s3_gate_cap_is_transient_then_permanent(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "import time\ndef test_slow():\n    time.sleep(30)\n"})
    gates.KILL_GRACE_S = 1
    r = m(gate("tests/test_a.py", cap=2), root, tmp_path)
    assert r.outcome == "unmeasured" and not r.transient and "twice" in r.reason
    assert r.duration_s < 10


def test_collection_error_recorded_per_file(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "def test_ok():\n    pass\n",
                              "tests/test_b.py": "import nope_not_a_module\n"})
    r = m(gate("tests"), root, tmp_path)
    assert r.outcome == "fail"
    assert "tests/test_b.py::<collect>" in r.failures
    assert "tests/test_a.py::test_ok" not in r.failures


def test_nonzero_exit_with_no_failures_is_a_failure(tmp_path):
    root = project(tmp_path, {
        "tests/test_a.py": "def test_ok():\n    pass\n",
        "tests/conftest.py": "def pytest_sessionfinish(session, exitstatus):\n"
                             "    session.exitstatus = 1\n"})
    r = m(gate("tests/test_a.py"), root, tmp_path)
    assert r.outcome == "fail" and r.reason == "nonzero-exit-no-failures"
    assert gates.judge(r, base=r.failure_map()).outcome == "pass"  # same at base


def test_s21_timeout_class_failure_rerun_alone(tmp_path):
    marker = tmp_path / "tmp" / "seen"
    body = (f"import os, pytest\nM = {str(marker)!r}\n"
            "def test_flaky():\n"
            "    if not os.path.exists(M):\n"
            "        open(M, 'w').close()\n"
            "        pytest.fail('Timeout >15.0s')\n"
            "def test_ok():\n    pass\n")
    root = project(tmp_path, {"tests/test_a.py": body})
    r = m(gate("tests/test_a.py"), root, tmp_path)
    assert r.outcome == "pass" and r.failures == {}


def test_s8_expected_reds(tmp_path):
    root = project(tmp_path, {"tests/__init__.py": ""})
    base = m(gate("tests/test_new.py"), root, tmp_path).failure_map()
    (root / "tests/test_new.py").write_text("def test_new():\n    assert False\n")
    r = m(gate("tests/test_new.py"), root, tmp_path)
    assert gates.judge(r, base, expect_red=["tests/test_new.py::test_new"]).outcome == "pass"
    assert gates.judge(r, base).outcome == "fail"
    (root / "tests/test_new.py").write_text("def test_new():\n    assert True\n")
    r2 = m(gate("tests/test_new.py"), root, tmp_path)
    assert gates.judge(r2, base, expect_red=["tests/test_new.py::test_new"]).outcome == "fail"
    assert gates.judge(r2, base).outcome == "pass"


def test_s25_pinned_env_hides_host_ilk_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ILK_SKILL_HOME", "/Users/x/.ilk")
    monkeypatch.setenv("ILK2_DATA", "/Users/x/.ilk2")
    monkeypatch.setenv("SOME_SECRET", "s")
    body = ("import os\ndef test_env():\n"
            "    assert not [k for k in os.environ if k.startswith(('ILK_', 'ILK2_'))]\n"
            "    assert 'SOME_SECRET' not in os.environ\n"
            "    assert not os.path.exists(os.path.expanduser('~/.ilk-data'))\n"
            "    assert os.listdir(os.path.expanduser('~')) == []\n")
    root = project(tmp_path, {"tests/test_a.py": body})
    r = m(gate("tests/test_a.py"), root, tmp_path)
    assert r.outcome == "pass", r.tail


def test_declared_ilk_var_allowed(tmp_path):
    body = "import os\ndef test_env():\n    assert os.environ['ILK_TEST_TOOLKIT_ROOT'] == '/x'\n"
    root = project(tmp_path, {"tests/test_a.py": body})
    r = m(gate("tests/test_a.py"), root, tmp_path, set_vars={"ILK_TEST_TOOLKIT_ROOT": "/x"})
    assert r.outcome == "pass", r.tail


def test_spec_version_tracks_config(tmp_path):
    root = project(tmp_path, {"tests/test_a.py": "def test_ok():\n    pass\n",
                              "tests/conftest.py": "# v1\n"})
    env = env_for(tmp_path)
    g = gate("tests/test_a.py")
    v1 = gates.spec_version(g, root, env)
    (root / "pytest.ini").write_text("[pytest]\n")
    v2 = gates.spec_version(g, root, env)
    (root / "tests/conftest.py").write_text("# v2\n")
    v3 = gates.spec_version(g, root, env)
    assert len({v1, v2, v3}) == 3
    assert gates.spec_version(g, root, env | {"HOME": "/elsewhere"}) == v3


def test_must_pass_requires_a_passing_test_not_just_no_failure(tmp_path):
    # the file fails to collect: test_new is never a "failure", but it did not pass either
    root = project(tmp_path, {"tests/test_new.py": "from nowhere import thing\n"
                                                    "def test_new():\n    assert thing\n"})
    r = m(gate("tests/test_new.py"), root, tmp_path)
    base = r.failure_map()  # same collect error at the base: not new
    v = gates.judge(r, base, must_pass=["tests/test_new.py::test_new"])
    assert v.outcome == "fail" and "tests/test_new.py::test_new" in v.new_failures


def test_must_pass_file_entry_means_every_test_in_it(tmp_path):
    root = project(tmp_path, {"tests/test_f.py": "def test_a():\n    pass\ndef test_b():\n    assert 0\n"})
    r = m(gate("tests/test_f.py"), root, tmp_path)
    assert gates.judge(r, r.failure_map(), must_pass=["tests/test_f.py"]).outcome == "fail"
    (root / "tests/test_f.py").write_text("def test_a():\n    pass\ndef test_b():\n    pass\n")
    r2 = m(gate("tests/test_f.py"), root, tmp_path)
    assert gates.judge(r2, r.failure_map(), must_pass=["tests/test_f.py"]).outcome == "pass"
    (tmp_path / "x").mkdir()
    root2 = project(tmp_path / "x", {"tests/__init__.py": ""})
    r3 = m(gate("tests/test_none.py"), root2, tmp_path)  # file absent: nothing passed
    assert gates.judge(r3, {}, must_pass=["tests/test_none.py"]).outcome == "fail"


def test_must_pass_file_entry_rejects_skipped_and_xfailed(tmp_path):
    # Codex M1 review #6: "every test in the file" must not accept a still-xfailed or skipped test.
    body = ("import pytest\ndef test_a():\n    pass\n"
            "@pytest.mark.xfail(strict=False)\ndef test_b():\n    assert 0\n")
    root = project(tmp_path, {"tests/test_f.py": body})
    r = m(gate("tests/test_f.py"), root, tmp_path)
    assert gates.judge(r, r.failure_map(), must_pass=["tests/test_f.py"]).outcome == "fail"
    (root / "tests/test_f.py").write_text(body.replace("@pytest.mark.xfail(strict=False)",
                                                       "@pytest.mark.skip"))
    r2 = m(gate("tests/test_f.py"), root, tmp_path)
    assert gates.judge(r2, r.failure_map(), must_pass=["tests/test_f.py"]).outcome == "fail"


def test_shell_gate_is_exit_code_only(tmp_path):
    root = project(tmp_path, {"ok.txt": ""})
    ok = Gate(name="lint", adapter="shell", command=["python", "-c", "pass"], declares_tests=False)
    bad = Gate(name="lint", adapter="shell", command=["python", "-c", "raise SystemExit(2)"],
               declares_tests=False)
    assert m(ok, root, tmp_path).outcome == "pass"
    r = m(bad, root, tmp_path)
    assert r.outcome == "fail" and list(r.failures) == ["lint::<exit>"]
    missing = Gate(name="lint", adapter="shell", command=["no-such-tool-xyz"], declares_tests=False)
    assert m(missing, root, tmp_path).outcome == "unmeasured"


def test_step_obligations_span_gates(tmp_path):
    # Found while fixing #7: must_pass and expected reds belong to the step, not to each gate.
    root = project(tmp_path, {"tests/test_a.py": "def test_a():\n    pass\n",
                              "tests/test_b.py": "def test_b():\n    assert 0\n"})
    ra = m(gate("tests/test_a.py", name="a"), root, tmp_path)
    rb = m(gate("tests/test_b.py", name="b"), root, tmp_path)
    unmet, missing = gates.unmet_obligations([ra, rb], ["tests/test_b.py::test_b"],
                                             ["tests/test_a.py::test_a"])
    assert unmet == [] and missing == []
    unmet, missing = gates.unmet_obligations([ra], ["tests/test_b.py::test_b"],
                                             ["tests/test_b.py::test_b"])
    assert unmet == ["tests/test_b.py::test_b"] and missing == ["tests/test_b.py::test_b"]
