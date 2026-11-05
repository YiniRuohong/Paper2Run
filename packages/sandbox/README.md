# p2r-sandbox

The Docker execution target used by Paper2Run's autonomous mode. Stdlib only,
Python >= 3.11, no imports from other `p2r_*` packages, so it can be installed
on its own with `pip install packages/sandbox`.

```python
from p2r_sandbox.autonomous import ExecutionTarget, ExecutionError

target = ExecutionTarget({
    "kind": "local",                      # or "ssh" with "ssh_alias"
    "workspace": "/abs/dedicated/dir",    # bind-mounted at /work
    "image": "paper2run-experiment:1",
    "network": {"public_internet": False, "host_and_metadata_access": False},
}, state_dir)
target.probe()      # Docker reachable? image present? container/network verified?
target.prepare()    # create or re-verify the container (also run before launches and file access)
target.start("python train.py", launch_id="run-1", timeout_seconds=3600)
```

- One container per target configuration (`p2r-<hash>`), on its own Docker
  network, with all capabilities dropped except the few the in-container
  controller needs, `no-new-privileges` and a PID limit.
- Each background job runs under its own never-reused UID (20000-60000, group
  10001) and file operations run as UID 10001, all without capabilities; a
  root-owned controller (`autonomous/_container.py`, copied in over stdin)
  enforces the network policy and serves files, jobs and logs.
- Host and cloud-metadata addresses are always blocked; public internet is an
  explicit per-target choice. The policy is re-verified before every launch or
  workspace access.
- SSH targets use `BatchMode`, `StrictHostKeyChecking=yes` and no agent or port
  forwarding; unknown or changed host keys are refused.
- The image is built with `bash scripts/autonomous/build-image.sh`; it is never
  pulled automatically.

Design and threat model: `docs/components/sandbox.md`.
