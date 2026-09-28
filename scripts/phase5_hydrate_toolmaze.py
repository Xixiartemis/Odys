"""Hydrate and verify the frozen ToolMaze benchmark for CI.

This is intentionally provider-free.  It materializes only the benchmark
inputs required by the Phase5 native tests (the 2,000 perturbed task JSONs)
and clones the official runtime/evaluator at the locked Git commit.

The dataset digest is the historical Phase5 digest: SHA-256 over the bytes of
all ``perturbed_tasks/**/*.json`` files in sorted POSIX path order.  The
individual downloads are fetched at an immutable Hugging Face dataset commit;
the aggregate digest is the final acceptance check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


TOOLMAZE_REPOSITORY = "https://github.com/Zhudongsheng75/ToolMaze.git"
TOOLMAZE_COMMIT = "ef0798aa7f31ac9b33403254b1ef76e8673305fa"
DATASET_REPOSITORY = "https://huggingface.co/datasets/dongsheng/ToolMaze"
DATASET_REVISION = "08b0239a98b0b07839d673f5bc0ef7d836db9296"
DATASET_SHA256 = "9fcd7d7ec3c06afcee098877cca1ae8c29b87a1102be4b365a1055f824a76bc8"
PERTURBED_FILE_COUNT = 2000


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _git_head(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _hydrate_code(repo_root: Path) -> None:
    if not (repo_root / ".git").is_dir():
        _run(["git", "init", str(repo_root)])
        _run(["git", "-C", str(repo_root), "remote", "add", "origin", TOOLMAZE_REPOSITORY])
    else:
        remotes = subprocess.check_output(
            ["git", "-C", str(repo_root), "remote"], text=True
        ).split()
        if "origin" not in remotes:
            _run(["git", "-C", str(repo_root), "remote", "add", "origin", TOOLMAZE_REPOSITORY])

    try:
        current = _git_head(repo_root)
    except subprocess.CalledProcessError:
        current = ""
    if current != TOOLMAZE_COMMIT:
        _run(["git", "-C", str(repo_root), "fetch", "--depth", "1", "origin", TOOLMAZE_COMMIT])
        _run(["git", "-C", str(repo_root), "checkout", "--detach", TOOLMAZE_COMMIT])

    actual = _git_head(repo_root)
    if actual != TOOLMAZE_COMMIT:
        raise RuntimeError(f"ToolMaze commit mismatch: expected {TOOLMAZE_COMMIT}, got {actual}")


def _hydrate_dataset(data_root: Path) -> None:
    """Fetch the dataset revision once, with a sparse checkout."""
    with tempfile.TemporaryDirectory(prefix="odys-toolmaze-dataset-") as temporary:
        checkout = Path(temporary) / "dataset"
        _run([
            "git", "clone", "--filter=blob:none", "--no-checkout",
            DATASET_REPOSITORY, str(checkout),
        ])
        _run(["git", "-C", str(checkout), "sparse-checkout", "init", "--cone"])
        _run(["git", "-C", str(checkout), "sparse-checkout", "set", "perturbed_tasks"])
        _run([
            "git", "-C", str(checkout), "checkout", "--detach", DATASET_REVISION,
        ])
        actual = _git_head(checkout)
        if actual != DATASET_REVISION:
            raise RuntimeError(f"dataset revision mismatch: expected {DATASET_REVISION}, got {actual}")

        source = checkout / "perturbed_tasks"
        if not source.is_dir():
            raise RuntimeError(f"dataset checkout missing {source}")
        destination = data_root / "perturbed_tasks"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True)


def _dataset_digest(data_root: Path) -> tuple[int, str]:
    files = sorted(
        (data_root / "perturbed_tasks").rglob("*.json"),
        key=lambda path: path.relative_to(data_root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return len(files), digest.hexdigest()


def hydrate(benchmark_root: Path, *, verify_only: bool = False) -> dict[str, str | int]:
    benchmark_root = benchmark_root.resolve()
    data_root = benchmark_root / "data"
    if not verify_only:
        benchmark_root.mkdir(parents=True, exist_ok=True)
        _hydrate_code(benchmark_root)
        _hydrate_dataset(data_root)

    count, digest = _dataset_digest(data_root)
    if count != PERTURBED_FILE_COUNT:
        raise RuntimeError(f"perturbed task count mismatch: expected {PERTURBED_FILE_COUNT}, got {count}")
    if digest != DATASET_SHA256:
        raise RuntimeError(f"dataset digest mismatch: expected {DATASET_SHA256}, got {digest}")

    return {
        "toolmaze_commit": _git_head(benchmark_root),
        "dataset_revision": DATASET_REVISION,
        "dataset_file_count": count,
        "dataset_sha256": digest,
        "provider_executed": "NO",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=Path("experiments/phase5/benchmarks/toolmaze"),
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    report = hydrate(args.benchmark_root, verify_only=args.verify_only)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
