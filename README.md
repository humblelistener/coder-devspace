# coder-devspace

Prototype CLI: turn a devspace-like repo + optional `docker-compose.yml` into a Coder Kubernetes template.

## Quick start

Open in VS Code and run **Reopen in Container**. The devcontainer installs Python and `pyyaml` automatically.

```bash
./coder-devspace.py https://github.com/humblelistener/conductor.git --out ./coder-template
```

See `AGENTS.md` for architecture, conventions, and known limitations.
