"""Track completed stages and estimate remaining parallel work for a Web task."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic

STAGES = {
    "Stage1-Clean": "stage1",
    "Stage2-Chunk": "stage2",
    "Stage3-QA": "stage3",
    "Stage4-Tag": "stage4",
    "Postprocess": "final",
}
PARALLEL_STAGES = ("stage3", "stage4")


def completed_stages(output_dir: Path) -> list[str]:
    if (output_dir / "final.json").is_file():
        return list(STAGES.values())
    return [
        stage for stage in STAGES.values()
        if stage != "final" and (output_dir / f"{stage}.json").is_file()
    ]


@dataclass
class _BatchTiming:
    total: int
    finished: int
    baseline: int
    started_at: float
    latest_at: float

    def remaining(self, now: float) -> int | None:
        if self.finished == self.total:
            return 0
        samples = self.finished - self.baseline
        elapsed = self.latest_at - self.started_at
        if samples < 2 or elapsed < 10:
            return None
        seconds_per_batch = elapsed / samples
        if now - self.latest_at > max(45, 3 * seconds_per_batch):
            return None
        return max(1, round(seconds_per_batch * (self.total - self.finished)))


class TaskProgress:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.active_stages: set[str] = set()
        self._batches: dict[str, _BatchTiming] = {}

    def batch_finished(self, stage_name: str, finished: int, total: int) -> None:
        stage = STAGES[stage_name]
        now = monotonic()
        if finished == 0:
            self._batches[stage] = _BatchTiming(total, 0, 0, now, now)
        else:
            timing = self._batches[stage]
            timing.finished = finished
            timing.latest_at = now

    def reset_eta(self) -> None:
        now = monotonic()
        for timing in self._batches.values():
            timing.started_at = timing.latest_at = now
            timing.baseline = timing.finished

    def snapshot(self) -> dict:
        completed = completed_stages(self.output_dir)
        # A task is only 100% after its terminal status is committed.
        finished = [stage for stage in completed if stage != "final"]
        return {
            "progress": len(finished) / len(STAGES),
            "completed_stages": finished,
            "active_stages": [
                stage for stage in STAGES.values()
                if stage in self.active_stages and stage not in finished
            ],
            "remaining_seconds": self._remaining_seconds(finished),
        }

    def _remaining_seconds(self, finished: list[str]) -> int | None:
        if not {"stage1", "stage2"}.issubset(finished):
            return None
        now = monotonic()
        remaining = []
        for stage in PARALLEL_STAGES:
            if stage in finished:
                continue
            timing = self._batches.get(stage)
            estimate = timing.remaining(now) if timing else None
            if estimate is None:
                return None
            remaining.append(estimate)
        # QA and tagging execute together. The slower branch determines the finish.
        return max(remaining) if remaining and any(remaining) else None
