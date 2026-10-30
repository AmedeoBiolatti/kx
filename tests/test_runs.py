"""kx.run, kx exec, protocols, the board, compare, reap."""

import json
import sys
import pytest

import kx
from kx.cli.main import main as kx_main
from kx.ledger.config import KxError

from conftest import scored_run, run_kx


def test_run_score_board_compare(comp, capsys):
    a = scored_run("base", {"f0": 0.70, "f1": 0.71, "f2": 0.72})
    b = scored_run("better", {"f0": 0.80, "f1": 0.82, "f2": 0.81})
    capsys.readouterr()
    code, board = run_kx("board", capsys=capsys)
    assert code == 0 and [r["run"] for r in board["rows"]] == [b.name, a.name]
    assert board["rows"][1]["vs_best"]["verdict"] == "worse"
    code, cmp = run_kx("compare", a.name, b.name, capsys=capsys)
    assert code == 0 and cmp["comparisons"][0]["verdict"] == "better"
    other = kx.Protocol("holdout", metric="auc")
    c = scored_run("elsewhere", {"x": 0.5, "y": 0.6}, protocol=other)
    capsys.readouterr()
    code, cmp = run_kx("compare", a.name, c.name, capsys=capsys)
    assert code == 5 and not cmp["comparable"]


def test_experiment_refuses_dirty_probe_saves_patch(comp, capsys):
    (comp / "new.py").write_text("x = 1\n")
    with pytest.raises(KxError, match="uncommitted"):
        with kx.run("dirty"):
            pass
    with kx.run("probe", kind="probe") as r:
        pass
    assert (r.dir / "code" / "untracked.tar.gz").exists()


def test_exec_scores_json_and_tqdm(comp, capsys):
    script = ("import json, os, pathlib; d = pathlib.Path(os.environ['KX_RUN_DIR']); "
              "(d / 'scores.json').write_text(json.dumps([{'protocol': 'cv3', 'spec': {'metric': 'auc', 'folds': 'f1'}, "
              "'value': 0.75, 'breakdown': {'f0': 0.7, 'f1': 0.8}}]))")
    code = kx_main(["exec", "-H", "from exec", "--json", "--", sys.executable, "-c", script])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["exit"] == 0
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["scores"][0]["protocol"].startswith("cv3@") and show["scores"][0]["value"] == 0.75


def test_reference_protocol(comp, capsys):
    scored_run("a", {"f0": 0.7, "f1": 0.8})
    scored_run("b", {"x": 0.1, "y": 0.2}, protocol=kx.Protocol("side"))
    scored_run("c", {"x": 0.3, "y": 0.2}, protocol=kx.Protocol("side"))
    capsys.readouterr()
    code, st = run_kx("status", capsys=capsys)
    assert st["results"]["reference"]["name"] == "side" and st["results"]["reference"]["source"] == "inferred"
    assert any(x["kind"] == "no_reference" for x in st["needs_you"])
    code, _ = run_kx("protocol", "use", "cv3", capsys=capsys)
    code, st = run_kx("status", capsys=capsys)
    assert st["results"]["reference"]["source"] == "set" and st["results"]["best"][0]["hypothesis"] == "a"
    assert not any(x["kind"] == "no_reference" for x in st["needs_you"])


def test_stale_run_shows_and_reaps(comp, capsys):
    from kx.ledger.store import open_store
    with kx.run("dies") as r:
        pass
    with open_store() as st:
        st.update("runs", {"status": "running", "pid": 999999999}, id=r.id)
    code, s = run_kx("status", capsys=capsys)
    assert any(x["kind"] == "dead_run" for x in s["needs_you"])
    code, closed = run_kx("reap", capsys=capsys)
    assert closed[0]["run"] == r.name


def test_runs_list_the_curves_each_run_logs(comp, capsys, tmp_path):
    from kx.runs.progress import curve_names
    with kx.run("curves") as r:
        for i in range(3):
            r.step(i, loss=1.0 / (i + 1), acc=0.5 + i / 10)
    capsys.readouterr()
    code, rows = run_kx("runs", "--curves", capsys=capsys)
    assert code == 0 and rows[0]["curves"] == ["loss", "acc"]
    big = tmp_path / "big"
    big.mkdir()
    lines = [json.dumps({"step": 0, "early": 1.0})] + [json.dumps({"step": i, "loss": 0.5, "note": "x" * 200}) for i in range(1, 2000)]
    (big / "metrics.jsonl").write_text("\n".join(lines + [json.dumps({"step": 2000, "late": 2.0})]) + "\n")
    assert curve_names(big) == ["early", "loss", "late"]  # the start and the end of a long file
    assert curve_names(tmp_path / "none") == []


def test_parsed_metrics_are_reused_until_the_file_changes(tmp_path):
    from kx.runs.progress import artifacts, curves
    (tmp_path / "metrics.jsonl").write_text(json.dumps({"step": 0, "loss": 1.0, "ts": "2026-10-23T10:00:00+00:00"}) + "\n")
    t1, t2 = {}, {}
    first = curves(tmp_path, t1)
    assert curves(tmp_path, t2) is first and t2 == t1  # unchanged: the same parse
    with (tmp_path / "metrics.jsonl").open("a") as f:
        f.write(json.dumps({"step": 1, "loss": 0.5}) + "\n")
    assert curves(tmp_path) == {"loss": [[0, 1.0], [1, 0.5]]}  # grown: read again
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret")
    (tmp_path / "model.bin").write_bytes(b"12345")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / "inside").symlink_to(tmp_path / "model.bin")
    listed, _ = artifacts(tmp_path)
    assert listed == [{"path": "inside", "bytes": 5}, {"path": "model.bin", "bytes": 5}]  # a link out of the run is left out


def test_curves_come_from_the_whole_file_and_live_readers_get_only_whats_new(tmp_path):
    from kx.runs.progress import curve_summary, curves, sampled
    path = tmp_path / "metrics.jsonl"

    def write(lo, hi, restart=None):
        with path.open("a") as f:
            for i in range(lo, hi):
                step = i - restart if restart is not None and i >= restart else i
                rec = {"step": step, "loss": 1.0 / (i + 1), "pad": "x" * 200}
                if i == 777:
                    rec["loss"] = 99.0
                f.write(json.dumps(rec) + "\n")

    write(0, 12000)  # about 3 MB: past what used to be read (the last 2 MiB)
    assert curves(tmp_path)["loss"][0] == [0, 1.0]
    s = curve_summary(tmp_path)["loss"]
    assert s["points"] == 12000 and s["first"] == 1.0 and s["max"] == 99.0
    seen = sampled(tmp_path)
    assert len(seen["points"]["loss"]) < 2500 and any(p[1] == 99.0 for p in seen["points"]["loss"])  # cut, the spike kept

    def merge(old, new):
        if new["start"] == 0:
            return new
        keep = old["done"]["loss"]
        return {**new, "points": {"loss": old["points"]["loss"][:keep] + new["points"]["loss"]},
                "done": {"loss": keep + new["done"]["loss"]}}

    for lo, hi in ((12000, 12003), (12003, 12500), (12500, 30000)):
        write(lo, hi, restart=20000)
        with path.open("a") as f:
            f.write('{"step": 5, "lo')  # still being written
        new = sampled(tmp_path, seen["chunk"], seen["full"])
        assert new["start"] or new["chunk"] != seen["chunk"]
        if new["start"]:
            assert len(new["points"]["loss"]) < 100  # only the chunks from the first not yet full
        seen = merge(seen, new)
        assert seen["points"] == sampled(tmp_path)["points"]  # the same as reading it all again
        with path.open("a") as f:
            f.write('ss": 1, broken}\n')  # the line ends, not as JSON: skipped
    loss = seen["points"]["loss"]
    assert sum(b[0] < a[0] for a, b in zip(loss, loss[1:])) == 1  # the restart at record 20000 survives the cut


def test_runs_list_their_config_for_the_runs_table(comp, capsys):
    with kx.run("cfg", config={"lr": 0.01, "model": {"depth": 4}}, seed=3) as r:
        r.log(acc=0.9)
    capsys.readouterr()
    code, rows = run_kx("runs", "--config", capsys=capsys)
    assert code == 0 and rows[0]["config"] == {"lr": 0.01, "model": {"depth": 4}} and rows[0]["seed"] == 3
    assert rows[0]["metrics"]["acc"] == 0.9 and rows[0]["n"] == 1


def test_a_score_split_from_the_main_version_says_so(comp, capsys):
    """Run settings put in a protocol's spec make a new version per setting: kx says so on the run and in status."""
    import kx
    from conftest import scored_run
    for i in range(3):
        scored_run(f"main {i}", {"f0": 0.8, "f1": 0.81}, protocol=kx.Protocol("bench", metric="auc", games=64))
    odd = scored_run("odd", {"f0": 0.8, "f1": 0.82}, protocol=kx.Protocol("bench", metric="auc", games=128))
    code, show = run_kx("show", odd.name, capsys=capsys)
    note = next(n["text"] for n in show["notes"] if "not the main version" in n["text"])
    assert "3 score(s)" in note and "games: 64 -> 128" in note
    code, res = run_kx("wait", odd.name, capsys=capsys)
    assert res["events"][0]["result"]["split"][0]["main_scores"] == 3
    for g in (256, 512):
        scored_run(f"g{g}", {"f0": 0.8, "f1": 0.8}, protocol=kx.Protocol("bench", metric="auc", games=g))
    code, st = run_kx("status", capsys=capsys)
    churn = next(x for x in st["needs_you"] if x["kind"] == "protocol_churn")
    assert churn["ref"] == "bench" and "games" in churn["what"]
