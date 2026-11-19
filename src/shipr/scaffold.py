"""`shipr init`: render a starter shipr.yml. Detection only pre-fills the template; shipr itself
has no framework-specific behaviour."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = """\
# shipr configuration (reference: "Configuration" in the shipr README)
# Commit this file. Keep secrets out of it: use ${{VARIABLES}} from your shell or a git-ignored .env
version: 1
project:
  name: {name}

{build}
files:
  exclude: []   # added to built-in excludes (.git, .env*, keys, node_modules, *.log ...)
  protect: []   # e.g. ["uploads/**", "config.php"]: never overwritten or deleted remotely

hooks:
  remote_after: []     # run on the server after upload (needs SSH shell access)

defaults:
  provider: sftp
  sftp:
    port: 22
    username: deploy
    key_file: ~/.ssh/id_ed25519
    # password: ${{SHIPR_PASSWORD}}

environments:
  staging:
    remote_path: /var/www/staging
    sftp:
      host: staging.example.com
  production:
    branch: main
    confirm: true
    remote_path: /var/www/html
    sftp:
      host: example.com
"""

NPM_BUILD = 'build:\n  commands: ["npm ci", "npm run build"]\n  output: {out}\n'


def render(root: Path) -> str:
    return TEMPLATE.format(name=root.name, build=detect_build(root))


def detect_build(root: Path) -> str:
    pkg = root / "package.json"
    if pkg.is_file():
        return _node_build(pkg)
    if (root / "composer.json").is_file():
        return ("build:\n  # vendor/ is not in git, so install it before upload; remove\n"
                "  # this if the server runs composer itself (hooks.remote_after)\n"
                '  commands: ["composer install --no-dev --optimize-autoloader"]\n')
    return "# no build step: the repository is uploaded as-is (minus excludes)\n"


def _node_build(pkg: Path) -> str:
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    if "build" not in data.get("scripts", {}):
        return ("# no build script: the repo is uploaded as-is; install deps on the server\n"
                '# hooks.remote_after, e.g. ["npm ci --omit=dev", "pm2 restart app"]\n')
    if "next" in deps:
        return ("# Next.js: upload source and build on the server (needs SSH), or use a\n"
                "# static export (`output: 'export'` in next.config) with output: out\n"
                + NPM_BUILD.format(out="out"))
    return NPM_BUILD.format(out="build" if "react-scripts" in deps else "dist")
