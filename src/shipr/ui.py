"""How the engine talks to the person running it. The CLI passes click-backed callbacks; tests
pass recorders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import DeployRecord
from .planner import Plan

PLAN_LIST_LIMIT = 100


@dataclass
class UI:
    echo: Callable[[str], None] = print
    confirm: Callable[[str], bool] = lambda _msg: False  # safe default: never assume "yes"

    def warn(self, msg: str) -> None:
        self.echo(f"warning: {msg}")


def show_plan(ui: UI, plan: Plan, renames: dict[str, str]) -> None:
    for op in plan.uploads[:PLAN_LIST_LIMIT]:
        ui.echo(f"  upload  {op.path}  ({op.reason}{', backup first' if op.backup else ''})")
    if len(plan.uploads) > PLAN_LIST_LIMIT:
        ui.echo(f"  ... and {len(plan.uploads) - PLAN_LIST_LIMIT} more uploads")
    for op in plan.deletes:
        hint = f"  (renamed to {renames[op.path]})" if op.path in renames else ""
        ui.echo(f"  delete  {op.path}{hint}")
    for rel in plan.protected:
        ui.echo(f"  protect {rel}  (protected: left untouched)")
    ui.echo(f"{len(plan.uploads)} to upload, {len(plan.deletes)} to delete, "
            f"{plan.unchanged + len(plan.adopt)} unchanged, {len(plan.protected)} protected")


def show_result(ui: UI, record: DeployRecord) -> None:
    ui.echo(f"{record.status}: {len(record.uploaded)} uploaded, {len(record.deleted)} deleted, "
            f"{len(record.failed)} failed in {record.duration_s}s (deploy id {record.id})")
    for f in record.failed:
        ui.echo(f"  FAILED {f.path}: {f.error}")
