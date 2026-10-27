"""scripts/failure_drill.py only ever drives its own throwaway projects."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "failure_drill", Path(__file__).resolve().parents[1] / "scripts" / "failure_drill.py"
)
failure_drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(failure_drill)


@pytest.mark.parametrize(
    "name",
    ["docmind", "docmind-stack", "docmind-smoke", "docmind-relcheck", "docmind-drill-",
     "docmind-drill-1234567", "docmind-drill-zzzzzzzz", "x-docmind-drill-12345678"],
)
def test_refuses_every_project_that_is_not_a_generated_drill(name, tmp_path):
    assert not failure_drill.is_drill_project(name)
    with pytest.raises(SystemExit):
        failure_drill.Stack(name, tmp_path / "x.env")


def test_drill_commands_are_scoped_to_the_project_and_the_volume_free_override(tmp_path):
    stack = failure_drill.Stack("docmind-drill-0a1b2c3d", tmp_path / "x.env")
    assert stack.base[:4] == ["docker", "compose", "-p", "docmind-drill-0a1b2c3d"]
    files = [stack.base[i + 1] for i, arg in enumerate(stack.base) if arg == "-f"]
    assert [Path(f).name for f in files] == ["compose.yaml", "compose.release-check.yaml"]
    assert stack.container("web") == "docmind-drill-0a1b2c3d-web-1"


def test_work_dir_inside_the_repository_is_refused(monkeypatch):
    monkeypatch.setattr("sys.argv", ["failure_drill.py", "--work-dir",
                                     str(failure_drill.ROOT / "tmp-drill")])
    with pytest.raises(SystemExit):
        failure_drill.main()
    assert not (failure_drill.ROOT / "tmp-drill").exists()
