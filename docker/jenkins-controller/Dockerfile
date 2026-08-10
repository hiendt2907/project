# Jenkins controller, migrated in-cluster (k3s) 2026-08-10 — previously ran as a
# systemd service directly on the omni-k3s-vm host. In-cluster gives it real
# cluster DNS/networking (harbor.harbor.svc.cluster.local resolves, ClusterIPs
# routable) instead of the host-level insecure-registries/no-DNS workarounds
# needed when Jenkins lived outside the pod network. Docker builds run against
# a DinD sidecar (see jenkins-incluster.yaml) so `docker build`/`push` also run
# inside the pod's own network namespace, not the host's.
#
# Tool versions pinned to match what the VM install had (confirmed live via
# Jenkins script console 2026-08-10) so the pipeline behaves identically after
# the move — kubectl v1.36.2+k3s1, helm v3.21.3, istioctl 1.30.3, docker CLI
# 29.7.1, all linux/amd64 (omni-k3s-vm is x86_64).
FROM jenkins/jenkins:lts-jdk21

USER root

RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/v1.36.2/bin/linux/amd64/kubectl" && \
    chmod +x /usr/local/bin/kubectl && \
    curl -fsSL "https://get.helm.sh/helm-v3.21.3-linux-amd64.tar.gz" | \
      tar -xz -C /tmp && \
    mv /tmp/linux-amd64/helm /usr/local/bin/helm && \
    rm -rf /tmp/linux-amd64 && \
    curl -fsSL -o /tmp/istioctl.tar.gz \
      "https://github.com/istio/istio/releases/download/1.30.3/istioctl-1.30.3-linux-amd64.tar.gz" && \
    tar -xz -C /usr/local/bin -f /tmp/istioctl.tar.gz istioctl && \
    rm -f /tmp/istioctl.tar.gz && \
    curl -fsSL -o /tmp/docker.tgz \
      "https://download.docker.com/linux/static/stable/x86_64/docker-29.7.1.tgz" && \
    tar -xz -C /tmp -f /tmp/docker.tgz && \
    mv /tmp/docker/docker /usr/local/bin/docker && \
    rm -rf /tmp/docker /tmp/docker.tgz

USER jenkins
