"""Shared test helpers (imported by the end-to-end and integration suites)."""

from pathlib import Path

from shipr import config, engine
from shipr.models import DeployRecord
from shipr.ui import UI


class RecordingUI(UI):
    """Records output and answers confirmations from a scripted list (default: no)."""

    def __init__(self, answers=(), trust_hosts=False):
        self.lines: list[str] = []
        self.prompts: list[str] = []
        self.answers = list(answers)
        super().__init__(echo=self.lines.append, confirm=self._confirm,
                         trust_host=lambda host, key: trust_hosts)

    def _confirm(self, msg: str) -> bool:
        self.prompts.append(msg)
        return self.answers.pop(0) if self.answers else False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def load_env(repo: Path, name: str = "prod") -> tuple[config.Config, config.Environment]:
    cfg = config.load(repo / "shipr.yml")
    return cfg, cfg.env(name)


def deploy(repo: Path, ui: UI | None = None, ref: str | None = None, **opts) -> DeployRecord:
    cfg, env = load_env(repo)
    return engine.deploy(cfg, env, ref, engine.DeployOptions(**opts), ui or RecordingUI())
