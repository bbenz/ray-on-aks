# Security

## Reporting a vulnerability

Report security issues in this repository's samples to the repository owner. For
Microsoft products and services, follow the
[Microsoft Security Response Center](https://msrc.microsoft.com/create-report)
process instead of opening a public issue.

Report vulnerabilities in the upstream projects these samples use to their own
maintainers:

- [Ray security advisories](https://github.com/ray-project/ray/security/advisories)
- [KubeRay](https://github.com/ray-project/kuberay/security)
- [Kubernetes](https://kubernetes.io/docs/reference/issues-security/security/)

## What these samples are

These samples are development-environment exercises for the companion Ray on AKS
articles. They assume trusted users and trusted workload code.

**A Ray cluster isn't a security boundary between its users.** Anyone who can
reach the Ray Dashboard, Jobs API, or Ray Client can run code on the cluster.
The samples in `samples/security` narrow that exposure; they don't make Ray safe
for untrusted tenants, and they don't add transport encryption or per-job
isolation.

Read `docs/validation-status.md` before you rely on any control here. It records
exactly which checks ran against a live cluster and which didn't.

## Reporting expectations for this repository

Don't include cluster names, subscription IDs, node IP addresses, tokens,
kubeconfig files, or Ray authentication secrets in issues or pull requests. The
samples deliberately avoid printing token values; keep it that way when you
adapt them.
