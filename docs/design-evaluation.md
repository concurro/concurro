# Design review evaluation

`engine/design_eval.py` provides an operator-requested smoke test of design review using
four fresh runner turns. It is not part of normal issue intake and does not estimate
general model accuracy.

| Fixture | Expected decision |
| ---- | ---- |
| Accurate contribution workflow | Approve |
| Controller merges automatically | Revise |
| Readiness requires no CI | Revise |
| Implementation opens an already-ready PR | Revise |

The runner constructs a small engine-owned Git repository for each evaluation, independent
of the target's source. Expected decisions stay outside model context. The evaluator checks
the report contract and cited evidence and counts invalid outputs, missed defects, false
alarms and correct responses separately.

## Running an evaluation

Use an idle issue in a configured target and a configured model profile. This makes live
model calls and dispatches runner sessions, so it requires explicit operator authorization.
It temporarily owns the task without consuming its queued work; busy or unresolved tasks
are refused. No fixture review is published to GitHub and no target candidate is created.

```bash
python3 -m service --settings /absolute/path/to/settings.json evaluate OWNER/ISSUES ISSUE PROFILE
```

Run this where the service's private settings and owner-only control socket are available.
The existing runner workflow, gateway and permissions must be operational. Results are
written under `<state_dir>/evaluations/<id>/`: one JSON per fixture and `summary.json`.
Reports record model/profile, bundle identity and fixture digest. Evaluations use gateway
budget; they are not an offline test command or a replacement for live contribution pilots.

Offline regressions live in `engine/tests/test_design_eval.py`; run them through the
[development test commands](../README.md#development). Historical container/image calibration
runs are in Git history and are not reproducible using the current runner-only interface.
