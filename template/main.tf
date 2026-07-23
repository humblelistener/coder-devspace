terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
  }
}

data "coder_workspace" "me" {}

resource "kubernetes_config_map" "postgres-bind-initdb" {
  metadata {
    name      = "postgres-bind-initdb"
    namespace = "coder-workspaces"
  }
  data = {
    "001_init.sql" = "CREATE DATABASE conductor;\nCREATE USER conductor WITH PASSWORD 'conductor';\nGRANT ALL PRIVILEGES ON DATABASE conductor TO conductor;\nGRANT ALL PRIVILEGES ON SCHEMA public TO conductor;\nALTER DATABASE conductor OWNER TO conductor;\n"
  }
}

resource "kubernetes_persistent_volume_claim" "postgres-postgres-data" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-postgres-postgres-data"
    namespace = "coder-workspaces"
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "1Gi"
      }
    }
  }
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"
  auth = "token"
  startup_script = <<-EOT
    #!/bin/sh
    set -e
    mkdir -p /home/coder/repos
    if [ ! -d /home/coder/repos/ai-stack ]; then
      git clone --depth 1 --branch main https://github.com/humblelistener/ai-stack.git /home/coder/repos/ai-stack
    fi
    if [ ! -d /home/coder/repos/conductor ]; then
      git clone --depth 1 --branch main https://github.com/humblelistener/conductor.git /home/coder/repos/conductor
    fi
    cd /home/coder/repos/conductor
    go mod download || true
    go build -o /tmp/conductor .
    exec /tmp/conductor
  EOT
}

resource "coder_app" "app" {
  agent_id     = coder_agent.main.id
  slug         = "app"
  display_name = "app"
  url          = "http://localhost:3000"
  icon         = "/icon/apps.svg"
  share        = "owner"
}

resource "kubernetes_persistent_volume_claim" "home" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-home"
    namespace = "coder-workspaces"
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "10Gi"
      }
    }
  }
}

resource "kubernetes_deployment" "ai-stack" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-ai-stack"
    namespace = "coder-workspaces"
    labels = {
      "app" = "ai-stack"
      "coder.workspace_id" = data.coder_workspace.me.id
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        "app" = "ai-stack"
      }
    }
    template {
      metadata {
        labels = {
          "app" = "ai-stack"
        }
      }
      spec {
        container {
          name  = "ai-stack"
          image = "mcr.microsoft.com/devcontainers/go:1.23"
          command = ["sh", "-c", "export PORT=\"8080\"; mkdir -p /home/coder/repos; if [ ! -d \"/home/coder/repos/ai-stack\" ]; then git clone --depth 1 --branch main https://github.com/humblelistener/ai-stack.git /home/coder/repos/ai-stack; fi; cd /home/coder/repos/ai-stack; go mod download || true; go build -o /tmp/ai-stack .; exec /tmp/ai-stack"]
          env {
            name  = "PORT"
            value = "8080"
          }
          port {
            container_port = 8080
            name             = "api"
            protocol         = "TCP"
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "ai-stack" {
  metadata {
    name      = "ai-stack"
    namespace = "coder-workspaces"
  }
  spec {
    selector = {
      "app" = "ai-stack"
    }
    port {
      port        = 8080
      target_port = 8080
      name        = "api"
      protocol    = "TCP"
    }
  }
}

resource "kubernetes_deployment" "postgres" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-postgres"
    namespace = "coder-workspaces"
    labels = {
      "app" = "postgres"
      "coder.workspace_id" = data.coder_workspace.me.id
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        "app" = "postgres"
      }
    }
    template {
      metadata {
        labels = {
          "app" = "postgres"
        }
      }
      spec {
        volume {
          name = "postgres-postgres-data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.postgres-postgres-data.metadata[0].name
          }
        }
        volume {
          name = "postgres-bind-initdb"
          config_map {
            name = kubernetes_config_map.postgres-bind-initdb.metadata[0].name
          }
        }
        container {
          name  = "postgres"
          image = "postgres:16"
          env {
            name  = "POSTGRES_USER"
            value = "conductor"
          }
          env {
            name  = "POSTGRES_PASSWORD"
            value = "conductor"
          }
          env {
            name  = "POSTGRES_DB"
            value = "conductor"
          }
          port {
            container_port = 5432
            name             = "port-5432"
            protocol         = "TCP"
          }
          volume_mount {
            name       = "postgres-postgres-data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "postgres-bind-initdb"
            mount_path = "/docker-entrypoint-initdb.d"
            read_only  = true
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "postgres" {
  metadata {
    name      = "postgres"
    namespace = "coder-workspaces"
  }
  spec {
    selector = {
      "app" = "postgres"
    }
    port {
      port        = 5432
      target_port = 5432
      name        = "port-5432"
      protocol    = "TCP"
    }
  }
}

resource "kubernetes_deployment" "conductor" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-conductor"
    namespace = "coder-workspaces"
    labels = {
      "app" = "conductor"
      "coder.workspace_id" = data.coder_workspace.me.id
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        "app" = "conductor"
      }
    }
    template {
      metadata {
        labels = {
          "app" = "conductor"
        }
      }
      spec {
        volume {
          name = "home"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.home.metadata[0].name
          }
        }
        container {
          name  = "conductor"
          image = "mcr.microsoft.com/devcontainers/go:1.23"
          command = ["sh", "-c", coder_agent.main.init_script]
          env {
            name  = "CODER_AGENT_TOKEN"
            value = coder_agent.main.token
          }
          env {
            name  = "PORT"
            value = "3000"
          }
          env {
            name  = "DB_ENDPOINT"
            value = "postgres://conductor:conductor@postgres:5432/conductor?sslmode=disable"
          }
          env {
            name  = "AI_ENDPOINT"
            value = "http://ai-stack:8080"
          }
          port {
            container_port = 3000
            name             = "app"
            protocol         = "TCP"
          }
          volume_mount {
            name       = "home"
            mount_path = "/home/coder"
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "conductor" {
  metadata {
    name      = "conductor"
    namespace = "coder-workspaces"
  }
  spec {
    selector = {
      "app" = "conductor"
    }
    port {
      port        = 3000
      target_port = 3000
      name        = "app"
      protocol    = "TCP"
    }
  }
}
