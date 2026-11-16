"""Separate-process measurements; prepare/enqueue and result are outside run."""
import argparse
import gc
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from memory_workloads import KINDS, SIZES, parameters, plan, verify_value

MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parent


def worker(path, job_id, kind, size):
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    from storage import Database
    from jobs import Jobs
    with Database(path) as db:
        assert Jobs(db).run(job_id) == "done"
    seconds = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
    tracemalloc.stop()
    # Snapshot resource counters BEFORE loading the complete result.
    with Database(path) as db:
        jobs = Jobs(db)
        assert jobs.status(job_id)["revision"] == 1
        verify_value(kind, size, jobs.result(job_id))
        assert jobs.run(job_id) == "done"
    return {"kind": kind, "size": size, "python_bytes": peak,
            "rss_bytes": rss_bytes, "seconds": seconds, "result_checked": True}


def measure(mode):
    from catalog import Catalog
    from jobs import Jobs
    from samples import C, P
    from storage import Database
    records = []
    with tempfile.TemporaryDirectory(prefix="array-memory-bench-") as directory:
        for kind in KINDS:
            for size in SIZES:
                for round_index in range(3):
                    path = Path(directory) / f"{kind}-{size}-{round_index}.sqlite"
                    with Database(path) as db:
                        catalog, jobs = Catalog(db), Jobs(db)
                        catalog.save("work", plan(kind))
                        job_id = jobs.enqueue("work", parameters(size))
                        catalog.save("work", P(["rows", "base"], C(-1)))
                    command = [sys.executable, "-B", str(Path(__file__).resolve()),
                               "_run", str(path), str(job_id), kind, str(size)]
                    completed = subprocess.run(command, cwd=ROOT, text=True,
                                               capture_output=True, check=True)
                    record = json.loads(completed.stdout)
                    record["round"] = round_index + 1
                    records.append(record)
                    print(json.dumps({"measurement": record}, sort_keys=True), flush=True)
    summaries = []
    for kind in KINDS:
        small = [r for r in records if r["kind"] == kind and r["size"] == SIZES[0]]
        large = [r for r in records if r["kind"] == kind and r["size"] == SIZES[1]]
        growth = statistics.median(r["python_bytes"] for r in large) - statistics.median(
            r["python_bytes"] for r in small)
        group = small + large
        conforms = (max(r["python_bytes"] for r in group) <= 32 * MIB
                    and max(r["rss_bytes"] for r in group) <= 96 * MIB
                    and growth <= 4 * MIB)
        summaries.append({"kind": kind, "python_growth_bytes": growth,
                          "python_medians": [statistics.median(r["python_bytes"] for r in g)
                                             for g in (small, large)],
                          "conforms": conforms})
    print(json.dumps({"mode": mode, "summary": summaries}, sort_keys=True), flush=True)
    if mode == "target":
        assert all(s["conforms"] for s in summaries), "RUN_MEMORY_TARGET_NOT_MET"
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "target", "_run"))
    parser.add_argument("worker_args", nargs="*")
    args = parser.parse_args()
    if args.mode == "_run":
        path, job_id, kind, size = args.worker_args
        print(json.dumps(worker(path, int(job_id), kind, int(size)), sort_keys=True))
    else:
        assert not args.worker_args
        measure(args.mode)


if __name__ == "__main__":
    main()
