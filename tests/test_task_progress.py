"""Verify progress and ETA across parallel stages, pauses, and checkpoints."""

from kbrefiner.api import task_progress


def test_parallel_eta_requires_samples_from_each_unfinished_stage(tmp_path, monkeypatch):
    now = 0
    monkeypatch.setattr(task_progress, "monotonic", lambda: now)
    progress = task_progress.TaskProgress(tmp_path)
    for stage in ("stage1", "stage2"):
        (tmp_path / f"{stage}.json").write_text("{}")

    progress.batch_finished("Stage3-QA", 0, 6)
    progress.batch_finished("Stage4-Tag", 0, 5)
    now = 15
    progress.batch_finished("Stage3-QA", 1, 6)
    now = 30
    progress.batch_finished("Stage3-QA", 2, 6)
    progress.batch_finished("Stage4-Tag", 1, 5)
    assert progress.snapshot()["remaining_seconds"] is None

    now = 50
    progress.batch_finished("Stage4-Tag", 2, 5)
    assert progress.snapshot()["remaining_seconds"] == 75
    (tmp_path / "stage4.json").write_text("{}")
    assert progress.snapshot()["remaining_seconds"] == 60
    assert progress.snapshot()["progress"] == 0.6

    now = 100
    assert progress.snapshot()["remaining_seconds"] is None
    progress.reset_eta()
    now = 115
    progress.batch_finished("Stage3-QA", 3, 6)
    assert progress.snapshot()["remaining_seconds"] is None
    now = 130
    progress.batch_finished("Stage3-QA", 4, 6)
    assert progress.snapshot()["remaining_seconds"] == 30


def test_saved_result_alone_preserves_milestones_before_terminal_status(tmp_path):
    (tmp_path / "final.json").write_text("{}")
    assert task_progress.TaskProgress(tmp_path).snapshot()["progress"] == 0.8
    assert task_progress.completed_stages(tmp_path) == ["stage1", "stage2", "stage3", "stage4", "final"]
