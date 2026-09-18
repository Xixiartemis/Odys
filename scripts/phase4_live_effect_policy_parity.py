"""Provider-free Phase 4 effect-policy parity qualification.

This is the 02F gate. It reuses the exact 02D official runner construction,
but proves that the phase effect policy is installed by the shared runtime
tool-registry path rather than by script-local monkey-patching.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.phase4_live_no_progress_parity import qualify


EXPERIMENT_ID = "phase4-live-effect-policy-parity-02f"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results" / EXPERIMENT_ID


def qualify_02f(output: Path | None = None) -> dict[str, Any]:
    report = qualify(output, experiment_id=EXPERIMENT_ID)
    report["effect_policy_parity"] = {
        "shared_effect_policy_implementation": bool(
            report.get("shared_effect_policy_implementation")
        ),
        "script_local_monkeypatch": bool(report.get("script_local_monkeypatch")),
        "real_runner_uses_same_policy": bool(
            report.get("real_runner_uses_same_policy")
        ),
        "runtime_tool_policy_id": report.get("runtime_tool_policy_id"),
        "registry_policy_install_count": report.get(
            "registry_policy_install_count"
        ),
    }
    report["provider_executed"] = False
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "qualification.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualify", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.qualify:
        parser.error("only --qualify is supported; this gate never calls a real provider")
    report = qualify_02f(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("PHASE4_02F_EFFECT_POLICY_PARITY_COMPLETE")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
