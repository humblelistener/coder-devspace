#!/usr/bin/env python3
"""
Prototype CLI: resolve a devspace-like dependency graph and emit a Coder template.

Supports:
  - devspace.yaml for cross-repo dependencies and workspace port/app config
  - docker-compose.yml for local backing services (e.g. postgres)

Usage:
    python coder-devspace.py <repo-url-or-path> --out ./coder-template

Example:
    python coder-devspace.py https://github.com/humblelistener/conductor.git --out ./coder-template
"""

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_GO_IMAGE = "mcr.microsoft.com/devcontainers/go:1.23"
DEFAULT_POSTGRES_IMAGE = "postgres:16"


@dataclass
class Service:
    name: str
    repo_url: str
    branch: str
    local_path: Path
    config: dict
    env: dict = field(default_factory=dict)
    ports: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    compose_services: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    image: str = ""


def run(cmd, cwd=None, check=True):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def is_git_url(text: str) -> bool:
    return text.startswith(("http://", "https://", "git@", "git://"))


def repo_name_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def clone_repo(repo_url: str, branch: str, dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        "git", "clone", "--branch", branch, "--depth", "1", repo_url, str(dest)
    ])


def load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def parse_env(value) -> dict:
    """Parse a docker-compose environment value (dict or list of KEY=VALUE strings) into a dict."""
    if isinstance(value, dict):
        return {k: str(v) for k, v in value.items()}
    if isinstance(value, list):
        out = {}
        for item in value:
            if isinstance(item, dict):
                out.update({k: str(v) for k, v in item.items()})
            else:
                k, _, v = str(item).partition("=")
                out[k] = v
        return out
    return {}


def parse_ports(value) -> list[dict]:
    """Parse docker-compose ports into [{port, name, protocol}]."""
    out = []
    for p in value or []:
        if isinstance(p, (int, str)):
            s = str(p)
        elif isinstance(p, dict):
            continue
        else:
            continue
        if ":" in s:
            _, _, container = s.rpartition(":")
            out.append({"port": int(container), "name": f"port-{container}", "protocol": "TCP"})
        else:
            out.append({"port": int(s), "name": f"port-{s}", "protocol": "TCP"})
    return out


def parse_volumes(value, repo_path: Path) -> list[dict]:
    """Parse docker-compose volumes. Returns list of {type, source, target, repo_path}."""
    out = []
    for v in value or []:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) >= 2:
                source, target = parts[0], parts[1]
                if source == ".":
                    continue
                if source.startswith(".") or source.startswith("/"):
                    out.append({"type": "bind", "source": source, "target": target,
                                "repo_path": (repo_path / source).resolve()})
                else:
                    out.append({"type": "named", "source": source, "target": target})
    return out


def resolve_service(identifier: str, branch: str, overrides: dict, cache_dir: Path, seen: dict = None) -> Service:
    """Resolve a service from a git URL, local path, or override."""
    if seen is None:
        seen = {}

    source = overrides.get(identifier, identifier)

    if is_git_url(source):
        repo_url = source
        name = repo_name_from_url(source)
        local_path = cache_dir / f"{name}-{branch}"
        clone_repo(repo_url, branch, local_path)
    else:
        local_path = Path(source).resolve()
        repo_url = str(local_path)
        name = local_path.name

    if name in seen:
        return seen[name]

    config = load_yaml(local_path / "devspace.yaml") or {}
    name = config.get("name", name)

    service = Service(name=name, repo_url=repo_url, branch=branch,
                      local_path=local_path, config=config)
    seen[name] = service

    # Cross-repo dependencies from devspace.yaml.
    for dep in config.get("dependencies", []):
        dep_source = dep.get("source", {})
        dep_branch = dep.get("branch", dep_source.get("branch", "main"))

        if "git" in dep_source:
            dep_identifier = dep_source["git"]
        elif "path" in dep_source:
            dep_identifier = str((local_path / dep_source["path"]).resolve())
        else:
            dep_identifier = dep["name"]

        dep_service = resolve_service(dep_identifier, dep_branch, overrides, cache_dir, seen)

        dep_env = dict(dep_service.env)
        for var in dep.get("vars", []):
            dep_env[var["name"]] = var["value"]
        dep_service.env = dep_env

        service.dependencies.append(dep_service)

    # Service's own env/ports from devspace.yaml.
    dev = config.get("dev", {})
    service.env = {e["name"]: e["value"] for e in dev.get("env", [])}
    service.ports = [{"port": p["port"], "name": p.get("name", f"port-{p['port']}"),
                      "protocol": p.get("protocol", "TCP"), "app": p.get("app", False)}
                     for p in dev.get("ports", [])]
    service.image = config.get("image", "")

    # Local backing services from docker-compose.yml in the root repo.
    compose = load_yaml(local_path / "docker-compose.yml")
    if compose:
        compose_services = compose.get("services", {})
        for svc_name, svc_data in compose_services.items():
            if svc_name == service.name:
                # Main compose service: merge env/ports/volumes into the root service.
                service.env = {**parse_env(svc_data.get("environment")), **service.env}
                existing_ports = {p["port"] for p in service.ports}
                for cp in parse_ports(svc_data.get("ports")):
                    if cp["port"] not in existing_ports:
                        service.ports.append(cp)
                service.volumes.extend(parse_volumes(svc_data.get("volumes"), local_path))
                if svc_data.get("image"):
                    service.image = svc_data["image"]
                continue

            compose_svc = Service(
                name=svc_name,
                repo_url=repo_url,
                branch=branch,
                local_path=local_path,
                config={},
                env=parse_env(svc_data.get("environment")),
                ports=parse_ports(svc_data.get("ports")),
                image=svc_data.get("image", ""),
                volumes=parse_volumes(svc_data.get("volumes"), local_path),
            )
            service.compose_services.append(compose_svc)

    return service


def flatten_services(root: Service) -> list[Service]:
    """Return dependencies first, then compose services, then root."""
    flat = []
    seen = set()

    def walk(s: Service):
        if s.name in seen:
            return
        for d in s.dependencies:
            walk(d)
        for c in s.compose_services:
            walk(c)
        seen.add(s.name)
        flat.append(s)

    walk(root)
    return flat


def container_image(service: Service) -> str:
    if isinstance(service.image, str) and service.image:
        return service.image
    if service.name == "postgres":
        return DEFAULT_POSTGRES_IMAGE
    if isinstance(service.image, dict) and service.image.get("dockerfile"):
        return DEFAULT_GO_IMAGE
    return DEFAULT_GO_IMAGE


def service_command(service: Service) -> str | None:
    """Single-line shell command to start a Go service from source."""
    if service.name == "postgres":
        return None

    env_exports = " ".join(f'export {k}="{v}";' for k, v in service.env.items())
    repo_dir = f"/home/coder/repos/{service.name}"
    binary = f"/tmp/{service.name}"
    return (
        f'{env_exports} '
        f'mkdir -p /home/coder/repos; '
        f'if [ ! -d "{repo_dir}" ]; then '
        f'git clone --depth 1 --branch {service.branch} {service.repo_url} {repo_dir}; '
        f'fi; '
        f'cd {repo_dir}; '
        f'go mod download || true; '
        f'go build -o {binary} .; '
        f'exec {binary}'
    )


def hcl_str(value) -> str:
    """Naive HCL double-quoted string literal escape."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def generate_configmaps(services: list[Service]) -> tuple[list[str], dict]:
    """Generate Kubernetes ConfigMaps for bind-mounted compose volumes."""
    configmap_tf = []
    configmap_names = {}
    for svc in services:
        for vol in svc.volumes:
            if vol.get("type") != "bind":
                continue
            src_path = vol["repo_path"]
            if not src_path.exists():
                continue
            cm_name = f"{svc.name}-bind-{src_path.name}".replace(".", "-").replace("_", "-")
            configmap_names[(svc.name, vol["source"])] = cm_name

            files = {}
            if src_path.is_file():
                files[src_path.name] = src_path.read_text()
            elif src_path.is_dir():
                for f in src_path.iterdir():
                    if f.is_file():
                        files[f.name] = f.read_text()

            configmap_tf.append(f'resource "kubernetes_config_map" "{cm_name}" {{')
            configmap_tf.append('  metadata {')
            configmap_tf.append(f'    name      = {hcl_str(cm_name)}')
            configmap_tf.append(f'    namespace = kubernetes_namespace.workspace.metadata[0].name')
            configmap_tf.append('  }')
            configmap_tf.append('  data = {')
            for k, v in files.items():
                configmap_tf.append(f'    {hcl_str(k)} = {hcl_str(v)}')
            configmap_tf.append('  }')
            configmap_tf.append('}')
            configmap_tf.append('')
    return configmap_tf, configmap_names


def generate_terraform(services: list[Service], out_dir: Path, namespace_prefix: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    root = services[-1]

    main_tf = []
    main_tf.append('terraform {')
    main_tf.append('  required_providers {')
    main_tf.append('    coder = {')
    main_tf.append('      source  = "coder/coder"')
    main_tf.append('      version = "~> 2.0"')
    main_tf.append('    }')
    main_tf.append('    kubernetes = {')
    main_tf.append('      source  = "hashicorp/kubernetes"')
    main_tf.append('      version = "~> 2.25"')
    main_tf.append('    }')
    main_tf.append('  }')
    main_tf.append('}')
    main_tf.append('')
    main_tf.append('provider "coder" {}')
    main_tf.append('')
    main_tf.append('provider "kubernetes" {')
    main_tf.append('  config_path = var.use_kubeconfig ? "~/.kube/config" : null')
    main_tf.append('}')
    main_tf.append('')
    main_tf.append('data "coder_workspace" "me" {}')
    main_tf.append('')
    main_tf.append('resource "kubernetes_namespace" "workspace" {')
    main_tf.append('  metadata {')
    main_tf.append(f'    name = substr(lower(replace("{namespace_prefix}-${{data.coder_workspace.me.name}}", " ", "-")), 0, 63)')
    main_tf.append('  }')
    main_tf.append('}')
    main_tf.append('')

    # ConfigMaps for bind mounts.
    configmap_tf, configmap_names = generate_configmaps(services)
    main_tf.extend(configmap_tf)

    # PVCs for named volumes.
    for svc in services:
        for vol in svc.volumes:
            if vol.get("type") != "named":
                continue
            pvc_name = f"{svc.name}-{vol['source']}".replace("_", "-")
            main_tf.append(f'resource "kubernetes_persistent_volume_claim" "{pvc_name}" {{')
            main_tf.append('  wait_until_bound = false')
            main_tf.append('  metadata {')
            main_tf.append(f'    name      = "coder-${{data.coder_workspace.me.id}}-{pvc_name}"')
            main_tf.append(f'    namespace = kubernetes_namespace.workspace.metadata[0].name')
            main_tf.append('  }')
            main_tf.append('  spec {')
            main_tf.append('    access_modes = ["ReadWriteOnce"]')
            main_tf.append('    resources {')
            main_tf.append('      requests = {')
            main_tf.append('        storage = "1Gi"')
            main_tf.append('      }')
            main_tf.append('    }')
            main_tf.append('  }')
            main_tf.append('}')
            main_tf.append('')

    # Coder agent for root workspace.
    main_tf.append('resource "coder_agent" "main" {')
    main_tf.append('  os   = "linux"')
    main_tf.append('  arch = "amd64"')
    main_tf.append('  auth = "token"')
    main_tf.append('  connection_timeout = 1800')
    main_tf.append('  startup_script = <<-EOT')
    main_tf.append('    #!/bin/sh')
    main_tf.append('    set -e')
    main_tf.append('    mkdir -p /home/coder/repos')
    for svc in services[:-1]:
        if svc.name == "postgres" or container_image(svc) == DEFAULT_POSTGRES_IMAGE:
            continue
        main_tf.append(f'    if [ ! -d /home/coder/repos/{svc.name} ]; then')
        main_tf.append(f'      git clone --depth 1 --branch {svc.branch} {svc.repo_url} /home/coder/repos/{svc.name}')
        main_tf.append('    fi')
    main_tf.append(f'    if [ ! -d /home/coder/repos/{root.name} ]; then')
    main_tf.append(f'      git clone --depth 1 --branch {root.branch} {root.repo_url} /home/coder/repos/{root.name}')
    main_tf.append('    fi')
    main_tf.append(f'    cd /home/coder/repos/{root.name}')
    main_tf.append('    go mod download || true')
    main_tf.append(f'    go build -o /tmp/{root.name} .')
    main_tf.append(f'    exec /tmp/{root.name}')
    main_tf.append('  EOT')
    main_tf.append('}')
    main_tf.append('')

    # coder_app for root app port.
    for port in root.ports:
        if port.get("app"):
            name = port["name"]
            port_num = port["port"]
            main_tf.append(f'resource "coder_app" "{name}" {{')
            main_tf.append('  agent_id     = coder_agent.main.id')
            main_tf.append(f'  slug         = {hcl_str(name)}')
            main_tf.append(f'  display_name = {hcl_str(name)}')
            main_tf.append(f'  url          = {hcl_str(f"http://localhost:{port_num}")}')
            main_tf.append('  icon         = "/icon/apps.svg"')
            main_tf.append('  share        = "owner"')
            main_tf.append('  subdomain    = true')
            main_tf.append('}')
            main_tf.append('')

    # PVC for workspace home.
    main_tf.append('resource "kubernetes_persistent_volume_claim" "home" {')
    main_tf.append('  wait_until_bound = false')
    main_tf.append('  metadata {')
    main_tf.append('    name      = "coder-${data.coder_workspace.me.id}-home"')
    main_tf.append(f'    namespace = kubernetes_namespace.workspace.metadata[0].name')
    main_tf.append('  }')
    main_tf.append('  spec {')
    main_tf.append('    access_modes = ["ReadWriteOnce"]')
    main_tf.append('    resources {')
    main_tf.append('      requests = {')
    main_tf.append('        storage = "10Gi"')
    main_tf.append('      }')
    main_tf.append('    }')
    main_tf.append('  }')
    main_tf.append('}')
    main_tf.append('')

    # Deployments and services for every service.
    for svc in services:
        dep_name = svc.name
        image = container_image(svc)
        is_root = svc.name == root.name

        main_tf.append(f'resource "kubernetes_deployment" "{dep_name}" {{')
        main_tf.append('  wait_for_rollout = false')
        main_tf.append('  metadata {')
        main_tf.append(f'    name      = "coder-${{data.coder_workspace.me.id}}-{dep_name}"')
        main_tf.append(f'    namespace = kubernetes_namespace.workspace.metadata[0].name')
        main_tf.append('    labels = {')
        main_tf.append(f'      "app" = {hcl_str(dep_name)}')
        main_tf.append('      "coder.workspace_id" = data.coder_workspace.me.id')
        main_tf.append('    }')
        main_tf.append('  }')
        main_tf.append('  spec {')
        main_tf.append('    replicas = data.coder_workspace.me.start_count')
        main_tf.append('    selector {')
        main_tf.append('      match_labels = {')
        main_tf.append(f'        "app" = {hcl_str(dep_name)}')
        main_tf.append('      }')
        main_tf.append('    }')
        main_tf.append('    template {')
        main_tf.append('      metadata {')
        main_tf.append('        labels = {')
        main_tf.append(f'          "app" = {hcl_str(dep_name)}')
        main_tf.append('        }')
        main_tf.append('      }')
        main_tf.append('      spec {')
        main_tf.append('        affinity {')
        main_tf.append('          node_affinity {')
        main_tf.append('            required_during_scheduling_ignored_during_execution {')
        main_tf.append('              node_selector_term {')
        main_tf.append('                match_expressions {')
        main_tf.append('                  key      = "kubernetes.io/os"')
        main_tf.append('                  operator = "In"')
        main_tf.append('                  values   = ["linux"]')
        main_tf.append('                }')
        main_tf.append('              }')
        main_tf.append('            }')
        main_tf.append('          }')
        main_tf.append('        }')

        # Volumes for PVC and ConfigMaps.
        for vol in svc.volumes:
            if vol.get("type") == "named":
                vol_name = f"{svc.name}-{vol['source']}".replace("_", "-")
                main_tf.append('        volume {')
                main_tf.append(f'          name = {hcl_str(vol_name)}')
                main_tf.append('          persistent_volume_claim {')
                main_tf.append(f'            claim_name = kubernetes_persistent_volume_claim.{vol_name}.metadata[0].name')
                main_tf.append('          }')
                main_tf.append('        }')
            elif vol.get("type") == "bind":
                cm_name = configmap_names.get((svc.name, vol["source"]))
                if cm_name:
                    main_tf.append('        volume {')
                    main_tf.append(f'          name = {hcl_str(cm_name)}')
                    main_tf.append('          config_map {')
                    main_tf.append(f'            name = kubernetes_config_map.{cm_name}.metadata[0].name')
                    main_tf.append('          }')
                    main_tf.append('        }')

        if is_root:
            main_tf.append('        volume {')
            main_tf.append('          name = "home"')
            main_tf.append('          persistent_volume_claim {')
            main_tf.append('            claim_name = kubernetes_persistent_volume_claim.home.metadata[0].name')
            main_tf.append('          }')
            main_tf.append('        }')

        main_tf.append('        container {')
        main_tf.append(f'          name  = {hcl_str(dep_name)}')
        main_tf.append(f'          image = {hcl_str(image)}')
        main_tf.append('          resources {')
        main_tf.append('            requests = {')
        main_tf.append('              cpu    = "250m"')
        main_tf.append('              memory = "512Mi"')
        main_tf.append('            }')
        main_tf.append('            limits = {')
        main_tf.append('              cpu    = "2"')
        main_tf.append('              memory = "2Gi"')
        main_tf.append('            }')
        main_tf.append('          }')

        if is_root:
            main_tf.append('          command = ["sh", "-c", coder_agent.main.init_script]')
        else:
            cmd = service_command(svc)
            if cmd:
                main_tf.append(f'          command = ["sh", "-c", {hcl_str(cmd)}]')

        if is_root:
            main_tf.append('          env {')
            main_tf.append('            name  = "CODER_AGENT_TOKEN"')
            main_tf.append('            value = coder_agent.main.token')
            main_tf.append('          }')

        for k, v in svc.env.items():
            main_tf.append('          env {')
            main_tf.append(f'            name  = {hcl_str(k)}')
            main_tf.append(f'            value = {hcl_str(v)}')
            main_tf.append('          }')

        for port in svc.ports:
            main_tf.append('          port {')
            main_tf.append(f'            container_port = {port["port"]}')
            main_tf.append(f'            name             = {hcl_str(port["name"])}')
            main_tf.append(f'            protocol         = {hcl_str(port.get("protocol", "TCP"))}')
            main_tf.append('          }')

        if is_root:
            main_tf.append('          volume_mount {')
            main_tf.append('            name       = "home"')
            main_tf.append('            mount_path = "/home/coder"')
            main_tf.append('          }')

        for vol in svc.volumes:
            if vol.get("type") == "named":
                vol_name = f"{svc.name}-{vol['source']}".replace("_", "-")
            elif vol.get("type") == "bind":
                vol_name = configmap_names.get((svc.name, vol["source"]))
            else:
                continue
            if vol_name:
                main_tf.append('          volume_mount {')
                main_tf.append(f'            name       = {hcl_str(vol_name)}')
                main_tf.append(f'            mount_path = {hcl_str(vol["target"])}')
                if vol.get("type") == "bind":
                    main_tf.append('            read_only  = true')
                main_tf.append('          }')

        main_tf.append('        }')
        main_tf.append('      }')
        main_tf.append('    }')
        main_tf.append('  }')
        main_tf.append('}')
        main_tf.append('')

        if svc.ports:
            main_tf.append(f'resource "kubernetes_service" "{dep_name}" {{')
            main_tf.append('  metadata {')
            main_tf.append(f'    name      = {hcl_str(dep_name)}')
            main_tf.append(f'    namespace = kubernetes_namespace.workspace.metadata[0].name')
            main_tf.append('  }')
            main_tf.append('  spec {')
            main_tf.append('    selector = {')
            main_tf.append(f'      "app" = {hcl_str(dep_name)}')
            main_tf.append('    }')
            main_tf.append('    port {')
            for port in svc.ports:
                main_tf.append(f'      port        = {port["port"]}')
                main_tf.append(f'      target_port = {port["port"]}')
                main_tf.append(f'      name        = {hcl_str(port["name"])}')
                main_tf.append(f'      protocol    = {hcl_str(port.get("protocol", "TCP"))}')
            main_tf.append('    }')
            main_tf.append('  }')
            main_tf.append('}')
            main_tf.append('')

    (out_dir / "main.tf").write_text("\n".join(main_tf))

    vars_tf = [
        'variable "use_kubeconfig" {',
        '  type        = bool',
        '  default     = false',
        '  description = "Use host kubeconfig? Set false when Coder runs in-cluster (the CDE default)."',
        '}',
        '',
    ]
    (out_dir / "variables.tf").write_text("\n".join(vars_tf))

    readme = [
        f"# Coder template: {root.name}",
        "",
        "Generated by coder-builder.",
        "",
        "## Services",
    ]
    for svc in services:
        readme.append(f"- `{svc.name}`")
    readme.extend([
        "",
        "## Usage",
        "1. Push this directory as a Coder template.",
        "2. Create a workspace from the template.",
        "3. Open the workspace in VS Code; repos are cloned under `/home/coder/repos/`.",
    ])
    (out_dir / "README.md").write_text("\n".join(readme))


def main():
    parser = argparse.ArgumentParser(description="Generate a Coder template from a devspace-like repo.")
    parser.add_argument("source", help="Git URL or local path to the root repo")
    parser.add_argument("--out", required=True, help="Output directory for the generated template")
    parser.add_argument("--branch", default="main", help="Git branch to use")
    parser.add_argument("--namespace-prefix", default="coder", help="Prefix for the per-workspace Kubernetes namespace (<prefix>-<workspace-name>)")
    parser.add_argument("--override", action="append", default=[],
                        help="Override a dependency source, e.g. --override postgres=/path/to/postgres")
    parser.add_argument("--cache", default=None, help="Cache directory for cloned repos")
    args = parser.parse_args()

    overrides = {}
    for item in args.override:
        key, _, value = item.partition("=")
        overrides[key] = value

    cache_dir = Path(args.cache) if args.cache else Path(tempfile.mkdtemp(prefix="coder-builder-"))
    out_dir = Path(args.out).resolve()

    print(f"Resolving root service from: {args.source}")
    root = resolve_service(args.source, args.branch, overrides, cache_dir)
    services = flatten_services(root)

    print("Resolved services:")
    for svc in services:
        print(f"  - {svc.name}: {svc.repo_url}")

    print(f"Generating Coder template at: {out_dir}")
    generate_terraform(services, out_dir, args.namespace_prefix)
    print("Done.")


if __name__ == "__main__":
    main()
