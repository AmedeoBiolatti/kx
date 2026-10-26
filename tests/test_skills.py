"""The skills kx init puts in a repo (kx-explain, kx-catch-up, kx-video) and the video template's data step."""
import json
import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

import kx
from kx.setup import scaffold

from conftest import P, commit

SKILLS = Path(scaffold.__file__).parent / "skills"


def test_each_skill_says_what_it_is_for():
    names = sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").exists())
    assert names == ["kx-catch-up", "kx-explain", "kx-video"]
    for name in names:
        text = (SKILLS / name / "SKILL.md").read_text()
        head = re.match(r"---\n(.*?)\n---\n", text, re.S)
        assert head, name
        fields = dict(line.split(": ", 1) for line in head.group(1).splitlines())
        assert fields["name"] == name  # Codex and Claude Code find a skill by its folder and this name
        assert 50 < len(fields["description"]) <= 1024
    for f in (SKILLS / "kx-video" / "template").glob("*.py"):
        py_compile.compile(str(f), doraise=True)


def test_init_puts_the_skills_where_codex_and_claude_code_read_them(tmp_path):
    root = tmp_path / "repo"
    (root / ".agents" / "skills" / "mine").mkdir(parents=True)
    (root / ".agents" / "skills" / "mine" / "SKILL.md").write_text("my own skill\n")
    done = scaffold.write_skills(root)
    assert done and "kx-explain" in done[0]
    for name in ("kx-explain", "kx-catch-up", "kx-video"):
        mine = root / ".agents" / "skills" / name / "SKILL.md"
        assert mine.read_text() == (SKILLS / name / "SKILL.md").read_text()
        link = root / ".claude" / "skills" / name
        assert (link / "SKILL.md").read_text() == mine.read_text()  # through the link (or its copy)
    assert (root / ".agents" / "skills" / "kx-video" / "template" / "render_all.py").is_file()
    assert scaffold.write_skills(root) == []  # up to date: nothing said
    (root / ".agents" / "skills" / "kx-explain" / "SKILL.md").write_text("edited\n")
    assert scaffold.write_skills(root) and "edited" not in (root / ".agents/skills/kx-explain/SKILL.md").read_text()
    assert (root / ".agents" / "skills" / "mine" / "SKILL.md").read_text() == "my own skill\n"  # not kx's: left alone


def test_the_video_data_comes_from_kx(comp, tmp_path):
    """prep_data.py: the run, its lineage through parent links, its baseline and kx compare's verdict, curves, the
    findings that cite them; narration.py reads its numbers from that."""
    with kx.run("baseline", config={"lr": 0.1}) as a:
        for i in range(5):
            a.step(i, loss=1.0 / (i + 1))
        a.score(0.70, P, breakdown={"f0": 0.69, "f1": 0.70, "f2": 0.71})
    with kx.run("more features", parent=a.name) as b:
        for i in range(5):
            b.step(i, loss=0.8 / (i + 1))
        b.score(0.80, P, breakdown={"f0": 0.79, "f1": 0.80, "f2": 0.81})
    subprocess.run([sys.executable, "-m", "kx", "finding", "add", "features help", "--evidence", a.name, b.name],
                   check=True, capture_output=True)
    video = tmp_path / "video"
    import shutil
    shutil.copytree(SKILLS / "kx-video" / "template", video)
    env = {**os.environ, "KX": f"{sys.executable} -m kx"}
    p = subprocess.run([sys.executable, str(video / "prep_data.py")], cwd=comp, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    d = json.loads((video / "data.json").read_text())
    assert d["best"]["run"] == b.name and d["best"]["score"] == 0.80
    assert [r["run"] for r in d["lineage"]] == [a.name, b.name]
    assert d["baseline"]["run"] == a.name and d["comparison"]["verdict"] == "better"
    assert d["curves"][b.name]["loss"][0] == [0, 0.8]
    assert d["findings"][0]["evidence"] == [a.name, b.name]
    assert all(c.startswith("kx ") for c in d["from"])  # where every number came from
    n = subprocess.run([sys.executable, "-c", "import narration; print(narration.NARRATION['score_compare'])"],
                       cwd=video, capture_output=True, text=True)
    assert n.returncode == 0, n.stderr
    assert "0.8" in n.stdout and "0.7" in n.stdout  # said from the data, not typed
