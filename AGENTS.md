# Project: coder-devspace

## Intent

Build a prototype CLI that resolves a devspace-style dependency graph and emits a Coder Kubernetes template. The goal is to let each repo declare its dev environment (dependencies, ports, env vars) in a `devspace.yaml`, with local backing services (e.g. postgres) optionally defined in `docker-compose.yml`. The CLI walks the dependency tree and generates a Kubernetes-based Coder template.

## Architecture

- **Input**: a git URL or local path to a root repo.
- **`devspace.yaml`**: declares cross-repo dependencies, workspace ports, env vars, and startup command.
- **`docker-compose.yml`** (optional): declares local backing services for the root repo.
- **Output**: a Terraform module (`main.tf`, `variables.tf`, `README.md`) suitable for Coder.

## Key files

- `coder-devspace.py` — the CLI.
- `requirements.txt` — only `pyyaml`.
- `.devcontainer/devcontainer.json` — Python devcontainer with dependencies auto-installed.
- `template/` — sample generated output for the `conductor` repo.

## Conventions

- Services named `postgres` or using a `postgres:*` image are treated as database backing services; the CLI does not try to clone source for them.
- Go services are built from source at workspace startup using `mcr.microsoft.com/devcontainers/go:1.23`.
- Generated templates use the `coder` and `kubernetes` Terraform providers.
- `.venv/`, `.cache/`, `repos/`, `__pycache__/` are ignored.

## Running the CLI

```bash
./coder-devspace.py <repo-url-or-path> --out ./output --namespace coder-workspaces
```

## Example public repos

- `https://github.com/humblelistener/conductor` — root workspace with `devspace.yaml` and `docker-compose.yml`.
- `https://github.com/humblelistener/ai-stack` — cross-repo dependency.

## Known limitations

- Only supports a limited subset of `devspace.yaml` and `docker-compose.yml`.
- Does not build Docker images; Go services are compiled at runtime.
- Generated templates are prototypes and should be validated with `terraform validate` before use in production.
