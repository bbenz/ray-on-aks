# Contributing

## Ground rules

- **Don't record something as validated unless it ran.** `docs/validation-status.md`
  separates what executed against a cluster from what was only checked offline.
  Keep that separation when you add to it.
- **Preserve failures.** A failed run that's documented is more useful than a
  rerun that hides it. When you fix something, keep the description of what it
  was.
- **No environment-specific values.** No subscription IDs, tenant IDs, cluster
  names, resource groups, node IPs, tokens, or kubeconfig files in code, tests,
  fixtures, or docs. `tests/test_scripts.py` and `tests/test_scaling_verifier.py`
  check for the obvious ones.
- **Pin what runs.** Image digests, package versions, and the model revision are
  pinned on purpose. If you bump one, bump it everywhere and say why.

## Before you open a pull request

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r tests/requirements-dev.txt
./.venv/Scripts/python -m pytest
```

If you have the article drafts locally, also run the synchronization checks:

```powershell
$env:RAY_ON_AKS_DOCS = "<folder containing the article markdown>"
./.venv/Scripts/python -m pytest tests/test_docs_sync.py
```

## Changing a sample

The articles currently ship their own copy of the samples so they read
standalone. `tests/test_docs_sync.py::test_mirrored_samples_are_identical`
fails when the copies drift. Update both, or update the mapping if a file is
deliberately repo-only.

When you change a manifest, check whether a test already encodes the rule you're
about to break. Several of them exist because a live run failed:

- The GPU check and the inference sample must pass identical `runtime_env`
  definitions.
- An autoscaling head keeps its service account token; everything else doesn't.
- Ray's pinned ports must all be covered by the network policies.
- GPU work must wait for allocatable `nvidia.com/gpu`, not just a Ready node.

If you're intentionally changing one of those rules, change the test in the same
commit and explain the reasoning in the message.

## Adding a script

- Dot-source `scripts/Common.ps1` so native command failures are caught.
- Use `[CmdletBinding(SupportsShouldProcess)]` and call `ShouldProcess` before
  anything that changes state.
- Add comment-based help with at least `.SYNOPSIS` and `.EXAMPLE`.
- Never delete a resource the user didn't name.

## Style

`.editorconfig` covers indentation and line endings. All text files use LF; the
`.gitattributes` file enforces it, because CRLF in a shell script breaks it
inside a Linux container.
