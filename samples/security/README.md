# Security samples

Companion code for **Secure Ray clusters on AKS**.

Apply these to a development cluster with trusted users. They reduce exposure;
they don't make Ray a multi-tenant boundary. Read `/SECURITY.md` and
`docs/validation-status.md` first.

| File | Purpose | Live-tested |
| --- | --- | --- |
| `ray-t4-fixed-ports.patch.json` | Pins Ray's otherwise random internal ports so a NetworkPolicy can describe them. | Yes |
| `ray-t4-network-policies.yaml` | Namespace default-deny plus narrow Ray, DNS, operator, submitter, and metrics exceptions. | Yes |
| `ray-t4-egress-proxy.yaml` | Egress exception allowing Ray pods to reach proxy pods on TCP 3128. Policy only. | Yes |
| `egress-proxy.yaml` | A Squid forward proxy with a destination allowlist, plus its own policies. Supplies what the exception above points at. | Yes |
| `ray-t4-token-auth-proxy.patch.json` | Adds `authOptions.mode: token` and the proxy environment variables to the quickstart RayCluster. | Yes |
| `rayjob-submitter-label.patch.json` | Adds the `ray-submit: ray-t4` submitter label the policies require. | Yes |
| `check_ray_auth.py` | From the head pod: unauthenticated Jobs API must fail, authenticated must succeed, direct external TCP must be blocked. | Yes |
| `check_proxy.py` | Approved destinations must return 200 through the proxy; an unapproved destination must get an explicit 403. | Yes |
| `security_probe.py` | From a probe pod: DNS, allowed submitter path, and denied same- and cross-namespace paths. | Yes |
| `security-probe-job.yaml` | Template Job that runs `security_probe.py`. Copy it per probe and set `PROBE_MODE`/`HEAD_IP`. | Yes |

## Order matters

Apply policies and pin ports **before** creating the Ray pods.

```powershell
kubectl patch --local -f ..\quickstart\raycluster-t4.yaml --type json `
  --patch-file .\ray-t4-fixed-ports.patch.json -o yaml |
  Set-Content -Encoding utf8 .\raycluster-isolated.yaml

kubectl apply -f .\ray-t4-network-policies.yaml
kubectl apply -f .\egress-proxy.yaml          # optional: supplies the proxy
kubectl apply -f .\ray-t4-egress-proxy.yaml   # optional: allows reaching it
kubectl apply -f .\raycluster-isolated.yaml
```

`scripts/Protect-RayCluster.ps1` performs the same sequence, including the
foreground delete that a rerun needs.

## Submitter label

Default-deny blocks the RayJob submitter from reaching the head. Patch both
RayJob manifests instead of editing them by hand:

```powershell
kubectl patch --local -f ..\quickstart\rayjob-gpu-check.yaml --type json `
  --patch-file .\rayjob-submitter-label.patch.json -o yaml |
  Set-Content -Encoding utf8 .\rayjob-gpu-check-secured.yaml
```

## Validate, don't assume

`kubectl apply` succeeding is not an enforcement test. Check an allowed path and
a denied path, and test the head **pod IP** so a DNS failure isn't mistaken for
isolation. `security_probe.py` covers all three cases and prints one JSON line
per check.

Known limits of this set:

- Shared Ray tokens are not per-job identities and add no transport encryption.
- The proxy allowlists destinations; it doesn't inspect TLS or prevent
  exfiltration to an allowed host.
- Recreating a RayCluster with the same name needs `--cascade=foreground`;
  otherwise terminating pods briefly match the new cluster's selectors.
