#!/usr/bin/env python3
"""Driver: run ONLY the GROUP 37 remote-python-actions-plugin tests against
the live omnidev stack.

tests.py cannot be imported (module-level `_args` referenced under __main__)
and `python3 tests.py --group 37` aborts in the __main__ body at GROUP 12
(_check_mm_container expects compose project 'omnideploy', but the dev stack
runs as 'omnidev' — that module-level check runs unconditionally).

Instead, exec tests.py with the __main__ block STRIPPED (starts at
`if __name__ == "__main__":`, ends just before the first module-level
test(...) call). The remaining module-level body is only print() banners +
test(...) invocations, guarded by TEST_FILTER (set to "37" below) + one
`_args.group` guard (satisfied by the fake _args).

Requires: omnidev omniagent running with the actions plugin as remote
(plugins.yml source: remote, remote.yml tools/actions, omni-plugins
tools/actions cloned into /opt/omni/plugins/tools/.remote/actions),
DATABASE_URL set (omnidev.env does).
"""
import os
import sys

os.environ["TEST_FILTER"] = "37"

src = open("/opt/workspace/omni-deployer/scripts/tests.py", encoding="utf-8").read()

lines = src.split("\n")
main_start = next(i for i, l in enumerate(lines) if l.startswith('if __name__ == "__main__":'))
main_end = next(i for i in range(main_start + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t")))
assert "test(test_fn_12_file_upload)" in lines[main_end], f"unexpected main_end {main_end}: {lines[main_end]}"

stripped = lines[:main_start] + lines[main_end:]

ns = dict(globals())
ns["_args"] = type("Args", (), {"group": "37"})()

exec(compile("\n".join(stripped), "tests.py[stripped]", "exec"), ns)

print("\n" + "=" * 60)
print(f"GROUP 37 SUMMARY: run={ns.get('tests_run', 0)} pass={ns.get('tests_pass', 0)} fail={ns.get('tests_fail', 0)}")
print("=" * 60)
sys.exit(1 if ns.get("tests_fail") else 0)
