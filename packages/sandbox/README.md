# p2r-sandbox

Isolated local execution of untrusted Python scripts. This package is
self-contained (stdlib only, zero third-party dependencies, Python >= 3.11)
and can be used inside Paper2Run or standalone:

```bash
# standalone, straight from a checkout
PYTHONPATH=packages/sandbox python -m p2r_sandbox run script.py --json
PYTHONPATH=packages/sandbox python -m p2r_sandbox capabilities --json

# or install it
pip install packages/sandbox
```

## Library use

```python
from p2r_sandbox import SandboxLimits, run_python_sandbox, validate_output_files

result = run_python_sandbox(
    "print('hello')",
    limits=SandboxLimits(max_seconds=10, max_memory_mb=256),
    collect_files=("predictions.json", "*.csv"),
)
result.backend          # "seatbelt" | "bwrap" | "unshare" | "none"
result.network_isolated # True only when a live probe proved network denial
result.exit_code, result.stdout, result.stderr, result.files, ...
predictions, labels = validate_output_files(result.files)
```

`run_python_sandbox` never raises for guest-side failures - those are
reported through `SandboxResult` (exit code, stderr notes prefixed with
`[p2r_sandbox]`, `timed_out`, `truncated`). It raises `SandboxUnavailable`
only when the sandboxed process cannot be launched at all.

## CLI

```
python -m p2r_sandbox run script.py [--timeout S] [--max-memory-mb N]
                                    [--collect GLOB ...] [--json]
python -m p2r_sandbox capabilities [--json]
```

`--json` prints one stable JSON object (sorted keys) with `backend`,
`network_isolated`, `timed_out`, `exit_code`, decoded `stdout`/`stderr` and
the collected `files` name list. Exit codes: guest's positive exit code,
`1` for kills/timeouts, `2` for usage/launch failures.

## Capability keys (`sandbox_capabilities()`)

| key | meaning |
| --- | --- |
| `seatbelt` | macOS seatbelt profile verified live: network + outside-write denied |
| `sensitive_read_denied` | the same probe child verifiably failed to read an existing sensitive directory (EPERM/EACCES) |
| `rlimits` | at least one rlimit (CPU/NPROC/FSIZE/AS) could be applied in a child |
| `platform_walls` | Windows: wall clock + output caps + throwaway cwd only |
| `bubblewrap` | bwrap probe passed: fresh netns + outside-write denied |
| `unshare_net` | `unshare -rn` probe passed: network isolation only |

Keys are additive: treat unknown keys as new capability reports, never as a
fixed schema.

## Honest limits (summary)

- Reads are allowed host-wide except the sensitive-path deny list on the
  seatbelt backend; the Linux backends do **not** restrict reads at all.
- `unshare -rn` does **not** restrict writes outside the workdir (reported
  in stderr when it is the selected backend).
- On macOS `RLIMIT_AS` is unenforceable; memory is enforced reactively by
  sampling RSS (leader every ~15 ms, whole process tree every ~200 ms).
- A guest child that creates a *new session* escapes process-group kills.
- Full threat model and backend matrix: [`docs/components/sandbox.md`](../../docs/components/sandbox.md)
  (repository root) when used inside Paper2Run.
