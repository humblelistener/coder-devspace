# coder-devspace

Prototype CLI that reads a devspace-like `devspace.yaml` (and optional `docker-compose.yml`) from a repo and emits a Coder Kubernetes template.

## Install

### With VS Code Dev Containers

Open the repo in VS Code and run **Reopen in Container**. The devcontainer installs Python and `pyyaml` automatically.

### Manual

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python coder-devspace.py https://github.com/humblelistener/conductor.git --out ./coder-template
```

Inside the devcontainer the script is executable:

```bash
./coder-devspace.py https://github.com/humblelistener/conductor.git --out ./coder-template
```

The output is a Terraform module ready to be imported as a Coder template.

## What it does

1. Clones the root repo.
2. Parses `devspace.yaml` and recursively resolves git dependencies.
3. Reads `docker-compose.yml` for local backing services (e.g. postgres).
4. Generates `main.tf`, `variables.tf`, and `README.md` in the output directory.

## Example generated services

- `conductor` — the Coder workspace with the coder agent and app port.
- `postgres` — a backing database defined in `conductor/docker-compose.yml`.
- `ai-stack` — a cross-repo dependency defined in `conductor/devspace.yaml`.

## Notes

This is a prototype. It builds Go services from source at workspace startup using the `mcr.microsoft.com/devcontainers/go:1.23` image, rather than pre-building Docker images.
