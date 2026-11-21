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
    # trust an unknown server key? (host, "type SHA256:..."); separate from `confirm` so that
    # --yes can never accept a host key
    trust_host: Callable[[str, str], bool] = lambda _host, _key: False

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
    for rel, why in plan.blocked:
        ui.echo(f"  skip    {rel}  ({why}: left untouched)")
    ui.echo(f"{len(plan.uploads)} to upload, {len(plan.deletes)} to delete, "
            f"{plan.unchanged + len(plan.adopt)} unchanged, {len(plan.protected)} protected"
            + (f", {len(plan.blocked)} left untouched" if plan.blocked else ""))


def show_result(ui: UI, record: DeployRecord) -> None:
    ui.echo(f"{record.status}: {len(record.uploaded)} uploaded, {len(record.deleted)} deleted, "
            f"{len(record.failed)} failed in {record.duration_s}s (deploy id {record.id})")
    ui.echo(f"  files: {'done' if record.files_done else 'NOT finished'} · hooks: "
            f"{record.hooks} · manifest: {'saved' if record.manifest_saved else 'NOT saved'}")
    for f in record.failed:
        ui.echo(f"  FAILED {f.path}: {f.error}")
