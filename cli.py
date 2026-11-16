"""Small command entry points; all persistent work stays in a supplied DB."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from catalog import Catalog
from jobs import Jobs
from samples import create_frozen, native_program, offset
from storage import Database
from values import WarehouseError
import sdk2


def demo():
    with TemporaryDirectory(prefix="arraywarehouse-") as directory:
        path = Path(directory) / "warehouse.sqlite"
        create_frozen(path)
        with Database(path) as db:
            catalog, jobs = Catalog(db), Jobs(db)
            pinned = jobs.status(1)["revision"]
            assert pinned == 1 and catalog.revision("offset") == 2
            revision = catalog.save("offset", offset())
            assert revision == 3
            parameters = {"rows": [3, None], "base": 10}
            fresh = jobs.enqueue("offset", parameters)
            parameters["rows"][0] = 1000
            for job_id in (1, 2, 5, fresh):
                assert jobs.run(job_id) == "done"
        with Database(path) as db:
            jobs = Jobs(db)
            old = jobs.result(1)
            nested = jobs.result(2)
            new = jobs.result(fresh)
            assert old == [9, 9, None]
            assert nested == [[37, 57], [], [None, 37]]
            assert new == [13, None]
            assert jobs.result(3) == [] and jobs.result(6) is None
            try:
                jobs.result(4)
            except WarehouseError as error:
                assert (error.code, error.message) == ("unbound", "missing")
            else:
                raise AssertionError("historical failed job lost its error")
            native = sdk2.execute(native_program(), [[2, 2, None], 7])
            record = sdk2.execute(native_program(True), [[2, 2, None], 7])
            assert native == record == old
            return {"storage_version": db.version, "pinned_revision": pinned,
                    "old_job": old, "nested_job": nested, "new_job": new,
                    "sdk2_port": native}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    frozen = sub.add_parser("freeze")
    frozen.add_argument("database")
    run = sub.add_parser("run")
    run.add_argument("database")
    run.add_argument("job_id", type=int)
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(json.dumps(demo(), ensure_ascii=False, sort_keys=True))
    elif args.command == "freeze":
        create_frozen(args.database)
        print(json.dumps({"database": args.database, "version": 1}))
    else:
        with Database(args.database) as db:
            jobs = Jobs(db)
            state = jobs.run(args.job_id)
            try:
                response = {"state": state, "result": jobs.result(args.job_id)}
            except WarehouseError as error:
                response = {"state": state, "error": {"code": error.code,
                                                     "message": error.message}}
            print(json.dumps(response, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
