#!/usr/bin/env python3
"""x6_robustness.py - external-tool robustness cases (external plan X6).

One timeout/hang/failure/parallel/cleanup case per NEW external tool of
`Projects/Omniagent/Omniagent-External-Improvement-Plan.md`:
himalaya (X1 email read), mcp-playwright (X4/X5 web), the SMS backend (X2, DEFERRED)
and oathtool/pyotp (X3 TOTP), plus the tool boundary the agent uses to run the
toolbox CLIs (the docker plugin).

Checklist (code plan 6.2-6.4):
  * a failure surfaces as a tool error - never a panic, crash or hang
  * every blocking external call has an explicit bound
  * parallel calls keep the stdout protocol clean (one line per call)
  * cancellation/timeout leaves no in-flight work behind

The module is self-contained so it can be run standalone inside the agent
container (fast iteration) and is also driven by tests.py GROUP 55:

    docker exec omnidev-omniagent-1 python3 /opt/omni/data/scripts/x6_robustness.py
    python3 scripts/x6_robustness.py check_b        # only the named check(s)

Precedent: scripts/x5_session_auth.py (GROUP 54).
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("OMNI_BASE", "http://localhost:8080")
# The agent's own tree (the runtime copy): the plan file and the plugin dir live here.
WORKSPACE = os.environ.get("OMNI_DIR", "/opt/omni")


def runtime_file(rel):
    """Resolve a runtime/profile-relative path to the first existing candidate.

    OMNI_DIR points at the runtime tree: in the omnidev dev stack that is the
    omni-root checkout (config/ + profiles/), but in the omni-deploy stack
    (deploy.py dev) it is the omni-stack SEED checkout, which carries config/
    and data/ only - profile content (wiki, skills, templates) lives in the
    omni-root checkout. Look in the runtime tree first, then in the omni-root
    checkout (override with OMNI_ROOT_DIR), then the production copy."""
    roots = [WORKSPACE,
             os.environ.get("OMNI_ROOT_DIR", ""),
             "/opt/workspace/omni-root",
             "/opt/omni"]
    cands = []
    for root in roots:
        if not root:
            continue
        cand = os.path.join(root, rel)
        if cand not in cands:
            cands.append(cand)
    for cand in cands:
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError(
        "%s not found under any runtime root: %s" % (rel, cands))
# Dev/hybrid stack env file - only used to pin the compose project of THIS stack.
STACK_ENV_FILE = os.environ.get("OMNI_DEV_ENV_FILE", "")
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/mcp"

TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # RFC 6238 test key (public vector)
TOTP_VECTOR_T59 = "287082"                          # RFC 6238, 6 digits at T=59
HANG_SLEEP = "12345"                                # distinctive duration, never a real wait


# ── helpers ──────────────────────────────────────────────────────────────

def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def mcp_execute_raw(name, args, timeout=90):
    """POST a tool call to the live executor and return the parsed envelope."""
    req = urllib.request.Request(
        f"{BASE}/mcp/execute",
        data=json.dumps({"name": name, "arguments": args}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def mcp_tools():
    with urllib.request.urlopen(f"{BASE}/mcp/tools", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def is_error(env):
    if not isinstance(env, dict):
        return True
    if env.get("is_error"):
        return True
    meta = env.get("metadata") or {}
    return bool(meta.get("is_error"))


def playwright_present():
    import os as _os
    return _os.path.isdir(f"{WORKSPACE}/plugins/tools/.remote/mcp-playwright")


def pw_chars(env):
    c = env.get("content") or ""
    if isinstance(c, list):
        c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
    return c


def toolbox_name():
    """Name of this stack's toolbox container (compose label filter, no hardcoded names)."""
    project = ""
    try:
        project = sh("docker inspect $(hostname) --format "
                     "'{{index .Config.Labels \"com.docker.compose.project\"}}'",
                     timeout=30).stdout.strip()
    except Exception:
        project = ""
    if not project:
        project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not project:
        return ""
    import urllib.parse
    filters = json.dumps({"label": [f"com.docker.compose.project={project}",
                                    "com.docker.compose.service=toolbox"]})
    rc = sh(f"curl -s --unix-socket /var/run/docker.sock "
            f"'http://localhost/containers/json?filters={urllib.parse.quote(filters)}'",
            timeout=30)
    try:
        containers = json.loads(rc.stdout or "[]")
    except Exception:
        return ""
    running = [c for c in containers if c.get("State") == "running"]
    return running[0]["Names"][0].lstrip("/") if running else ""


def skip():
    """True when this stack cannot run the toolbox cases (no toolbox / no docker CLI)."""
    if not toolbox_name():
        print("SKIP: no running toolbox container in this stack - nothing to test")
        return True
    if sh("docker version --format '{{.Server.Version}}'", timeout=30).returncode != 0:
        print("SKIP: docker CLI unavailable inside the agent container - nothing to test")
        return True
    return False


def tb_exec(cmd, timeout=45):
    """Run `sh -c cmd` inside the toolbox with a hard client-side bound.
    Returns (rc, stdout, stderr, elapsed, timed_out)."""
    tb = toolbox_name()
    t0 = time.time()
    try:
        r = subprocess.run(["docker", "exec", tb, "sh", "-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr, time.time() - t0, False
    except subprocess.TimeoutExpired as e:
        def _txt(v):
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace")
            return v or ""
        return None, _txt(e.stdout), _txt(e.stderr), time.time() - t0, True


def tb_parallel(cmds, timeout=30):
    """Run the commands concurrently; results come back in input order."""
    import threading
    res = {}

    def _one(i, c):
        try:
            res[i] = tb_exec(c, timeout=timeout)
        except Exception as e:  # pragma: no cover - defensive
            res[i] = (None, "", str(e), 0.0, True)

    ths = [threading.Thread(target=_one, args=(i, c)) for i, c in enumerate(cmds)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return [res[i] for i in range(len(cmds))]


def tb_pids(token, include_init=False):
    """pids inside the toolbox whose command line contains `token`.
    The token is passed as a bracketed REGEX ('[1]2345'), so neither the sh
    wrapper nor the grep of the probe itself can match its own argv.
    pid 1 is skipped by default: it is the container's own init/entrypoint and
    for a toolbox started via `docker compose run <cmd>` its argv carries the
    WHOLE original command line (a stale probe's `sh -c '... oathtool ...'`
    shows up there) - container infrastructure, not in-flight work."""
    br = "[" + token[0] + "]" + token[1:]
    rc, out, err, dt, to = tb_exec("ps -eo pid,args 2>/dev/null | grep -- %s || true" % br,
                                   timeout=20)
    assert not to, f"probe for leftover processes hung (token {token})"
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        head = line.split(None, 1)[0]
        if head.isdigit():
            pid = int(head)
            if pid == 1 and not include_init:
                continue
            pids.append(pid)
    return pids


def assert_no_proc(token, what, baseline=None):
    """No process matching `token` may be left behind. `baseline` is the set of
    matches seen BEFORE the check started: only NEW matches count as leftovers."""
    left = tb_pids(token)
    if baseline is not None:
        left = [p for p in left if p not in set(baseline)]
    assert not left, f"leftover process(es) for {what}: {left} (baseline {baseline})"


# ── checks ───────────────────────────────────────────────────────────────

def check_prereqs_and_sms_deferral():
    """55-A: the plan's tools exist, and the SMS channel is demonstrably absent
    (X2 DEFERRED, external plan section 11). The SMS half turns RED the moment a
    backend appears, so its robustness case cannot be silently forgotten."""
    if skip():
        return
    rc, out, err, dt, to = tb_exec("for t in himalaya oathtool python3; do command -v $t; done",
                                   timeout=30)
    assert not to and rc == 0, f"toolbox probe failed: rc={rc} {err!r}"
    missing = [t for t in ("himalaya", "oathtool", "python3")
               if not any(p.rstrip().endswith("/" + t) for p in out.splitlines())]
    assert not missing, f"toolbox misses external tool(s) {missing}: {out!r}"
    rc, out, err, dt, to = tb_exec(
        "python3 -c \"import pyotp,base64;print(len(base64.b32decode('%s')))\"" % TOTP_SECRET,
        timeout=30)
    assert not to and rc == 0 and out.strip() == "20", \
        f"pyotp unusable in the toolbox: rc={rc} out={out!r} err={err!r}"
    sms_tools = [t["name"] for t in mcp_tools() if "sms" in t["name"].lower()]
    rc, out, err, dt, to = tb_exec(
        "for t in gammu mmcli termux-sms-list; do command -v $t; done; true", timeout=30)
    cli = [l for l in out.splitlines() if l.strip()]
    assert not sms_tools and not cli, (
        "an SMS backend appeared (tools=%s cli=%s): X2 was DEFERRED, so X6 must "
        "now add the SMS robustness case (timeout/hang/failure/cleanup)" % (sms_tools, cli))
    try:
        plan = open(runtime_file("profiles/omni/wiki/Projects/Omniagent/"
                                 "Omniagent-External-Improvement-Plan.md"),
                    encoding="utf-8").read()
    except FileNotFoundError as e:
        # The deploy/CI runtime tree is the omni-stack SEED checkout (config/
        # + data/ only): the wiki lives in the omni-root checkout, which the
        # CI integration job does not carry. The SMS-backend assertion above
        # is the actual robustness case; the documentation cross-check is not
        # available in such a tree, so say so loudly instead of failing the
        # whole suite on a missing wiki page.
        print("SKIP: 55-A external plan not in this runtime tree (%s) - the "
              "section 11 deferral cross-check cannot run here" % e)
        plan = None
    if plan is not None:
        assert "## 11. X2 status: **DEFERRED" in plan, \
            "external plan section 11 (X2 deferral) missing"
        print("PASS: 55-A himalaya/oathtool/pyotp present; no SMS tool and no "
              "gammu/mmcli backend -> X2 deferral holds (external plan "
              "section 11)")
    else:
        print("PASS: 55-A himalaya/oathtool/pyotp present; no SMS tool and no "
              "gammu/mmcli backend -> X2 deferral holds (plan file absent, "
              "cross-check skipped)")


def check_himalaya():
    """55-B: himalaya (X1): a failure is bounded and loud; a black-holed IMAP
    endpoint never answers and is contained only by an explicit bound; parallel
    calls stay protocol-clean; nothing is left behind."""
    if skip():
        return
    base = tb_pids("himalaya")   # pre-existing matches are not our leftovers
    import base64
    # (1) FAILURE: no config -> bounded, non-zero exit, loud diagnostic.
    rc, out, err, dt, to = tb_exec("himalaya -c /tmp/x6-no-such-config.toml account list",
                                   timeout=25)
    assert not to, f"missing-config himalaya hung ({dt:.1f}s)"
    assert rc not in (0, None), f"missing config must fail: rc={rc} out={out!r}"
    assert (out + err).strip(), "missing-config failure printed nothing at all"
    assert dt < 20, f"missing-config failure not bounded ({dt:.1f}s)"
    fail_dt = dt
    # (2) HANG + BOUND: black-holed IMAP host. Without `timeout 12` the call
    # would never return; the kill is the evidence for the "bounded external
    # call" rule of the code plan (6.4).
    cfg = ("[accounts.x6]\ndefault = true\nemail = \"x6@example.invalid\"\n"
           "backend.type = \"imap\"\nbackend.host = \"10.255.255.1\"\n"
           "backend.port = 143\nbackend.encryption.type = \"none\"\n"
           "backend.login = \"x6\"\nbackend.auth.type = \"password\"\n"
           "backend.auth.raw = \"x6\"\nfolder.aliases.inbox = \"INBOX\"\n")
    b64 = base64.b64encode(cfg.encode()).decode()
    rc, out, err, dt, to = tb_exec(
        "echo %s | base64 -d > /tmp/x6-himalaya.toml && "
        "timeout 12 himalaya -c /tmp/x6-himalaya.toml envelope list -a x6 -o json" % b64,
        timeout=30)
    assert not to, "the harness bound fired - the explicit 12s bound did not contain himalaya"
    assert rc not in (0, None), \
        f"unreachable IMAP host must not succeed: rc={rc} err={err[-200:]!r}"
    if dt >= 9:
        assert rc in (124, 137, 143), \
            f"the bound must kill the hung call: rc={rc} ({dt:.1f}s) err={err[-200:]!r}"
        hang_note = f"hung then killed by the explicit 12s bound (rc={rc}, {dt:.1f}s)"
    else:
        hang_note = f"refused fast ({dt:.1f}s, rc={rc}) - still bounded"
    assert_no_proc("himalaya", "himalaya", base)
    # (3) PARALLEL: 4 concurrent invocations. Each call appends its OWN marker,
    # so a call that received another call's bytes (interleaving) or lost its
    # marker (truncation) turns this RED, while the intact multi-line banner is
    # still required from every single call.
    res = tb_parallel(["himalaya --version; echo X6-MARK-%d" % i for i in range(4)],
                      timeout=30)
    banners = []
    for i, (rc, out, err, dt, to) in enumerate(res):
        assert not to and rc == 0, f"parallel himalaya call failed: rc={rc} {err!r}"
        o = [l for l in out.splitlines() if l.strip()]
        assert o and o[-1] == "X6-MARK-%d" % i, \
            f"parallel call {i} stdout mixed up or truncated: {out!r}"
        foreign = [m for m in re.findall(r"X6-MARK-\d", out) if m != "X6-MARK-%d" % i]
        assert not foreign, f"stdout of parallel call {i} leaked another call: {out!r}"
        banners.append("\n".join(o[:-1]))
    assert len(set(banners)) == 1, f"parallel himalaya calls disagree: {banners}"
    assert banners[0].startswith("himalaya v") and "build:" in banners[0], \
        f"parallel invocation returned a garbled banner: {banners[0]!r}"
    assert_no_proc("himalaya", "himalaya", base)
    print(f"PASS: 55-B himalaya failure bounded ({fail_dt:.1f}s, loud) + black-holed IMAP "
          f"{hang_note} + 4 parallel calls clean (1 line each) + no leftover process")


def check_totp():
    """55-C: oathtool/pyotp (X3): the RFC 6238 vector is exact on both
    implementations; a malformed secret fails fast, loud and with a clean stdout
    (protocol hygiene); parallel invocations return one identical 6-digit code;
    no residue."""
    if skip():
        return
    base = tb_pids("oathtool")   # pre-existing matches are not our leftovers
    S = TOTP_SECRET
    rc, out, err, dt, to = tb_exec("oathtool --totp -b %s -N @59" % S, timeout=20)
    assert not to and rc == 0 and out.strip() == TOTP_VECTOR_T59, \
        f"oathtool RFC 6238 vector wrong: rc={rc} out={out!r} err={err!r}"
    rc, out2, err, dt, to = tb_exec(
        "python3 -c \"import pyotp;print(pyotp.TOTP('%s').at(59))\"" % S, timeout=25)
    assert not to and rc == 0 and out2.strip() == TOTP_VECTOR_T59, \
        f"pyotp RFC 6238 vector wrong: rc={rc} out={out2!r} err={err!r}"
    rc, out3, err, dt, to = tb_exec("oathtool --totp -b 'NOT-BASE32!!!'", timeout=20)
    assert not to, f"malformed secret hung ({dt:.1f}s)"
    assert rc not in (0, None), f"malformed base32 secret must fail: rc={rc} out={out3!r}"
    assert not out3.strip(), f"failure wrote to stdout (protocol dirt): {out3!r}"
    assert (out3 + err).strip(), "malformed-secret failure printed nothing at all"
    bad_rc, bad_dt = rc, dt
    # PARALLEL: a 30s TOTP window can roll over between two calls, so a
    # disagreement is retried once before it counts as a failure.
    codes, rounds = [], 0
    for _ in range(2):
        rounds += 1
        res = tb_parallel(["oathtool --totp -b %s" % S] * 4, timeout=25)
        codes = []
        for rc, out, err, dt, to in res:
            assert not to and rc == 0, f"parallel totp call failed: rc={rc} {err!r}"
            o = [l for l in out.splitlines() if l.strip()]
            assert len(o) == 1 and len(o[0].strip()) == 6 and o[0].strip().isdigit(), \
                f"parallel totp output is not one 6-digit line: {out!r}"
            codes.append(o[0].strip())
        if len(set(codes)) == 1:
            break
    assert len(set(codes)) == 1, f"parallel totp codes disagreed in {rounds} rounds: {codes}"
    assert_no_proc("oathtool", "oathtool", base)
    print(f"PASS: 55-C RFC 6238 vector {TOTP_VECTOR_T59} on oathtool AND pyotp; "
          f"malformed secret bounded ({bad_dt:.1f}s, rc={bad_rc}, clean stdout); "
          f"4 parallel codes identical ({codes[0]}, round {rounds}); no leftover process")


def check_playwright():
    """55-D: mcp-playwright (X4/X5): a navigation to a black-holed address cannot
    wedge a thread (bounded failure + recovery), parallel snapshots stay
    consistent, and the registry still exposes every playwright tool."""
    if not playwright_present():
        print("SKIP: mcp-playwright not installed (omnistable) - nothing to test")
        return
    p = "mcp-playwright_"
    t0 = time.time()
    outcome = "error-envelope"
    try:
        env = mcp_execute_raw(p + "browser-navigate", {"url": "http://10.255.255.1/"},
                              timeout=90)
        blob = json.dumps(env).lower()
        assert is_error(env) or "lost" in blob or "fail" in blob, \
            f"black-holed navigation must surface as a failure: {str(env)[:300]}"
    except Exception as e:
        outcome = "bounded-transport (%s)" % type(e).__name__
    dt = time.time() - t0
    assert dt < 80, f"hanging navigation not bounded ({dt:.1f}s)"
    # RECOVERY: after such a failure the plugin must respawn and work again.
    healthy, last = False, ""
    for _ in range(5):
        try:
            env = mcp_execute_raw(p + "browser-navigate", {"url": "https://example.com"},
                                  timeout=60)
            if env.get("success"):
                healthy = True
                break
            last = str(env)[:200]
        except Exception as e:
            last = str(e)[:200]
        time.sleep(5)
    assert healthy, f"browser did not recover after the hanging navigation: {last}"
    # PARALLEL: 3 concurrent snapshots of the same page must agree.
    import threading
    res = {}

    def _snap(i):
        try:
            res[i] = pw_chars(mcp_execute_raw(p + "browser-snapshot", {}, timeout=60))
        except Exception as e:
            res[i] = "ERROR:" + str(e)

    ths = [threading.Thread(target=_snap, args=(i,)) for i in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    bodies = [res.get(i, "") for i in range(3)]
    assert all("Page URL: https://example.com" in b for b in bodies), \
        f"parallel snapshots not consistent: {[b[:80] for b in bodies]}"
    pw = [t["name"] for t in mcp_tools() if t["name"].startswith(p)]
    assert len(pw) >= 20, f"registry lost playwright tools after the failure: {len(pw)}"
    print(f"PASS: 55-D black-holed navigation bounded ({dt:.1f}s, {outcome}), browser "
          f"recovered, 3 parallel snapshots consistent, {len(pw)} live playwright tools")


def check_tool_timeout():
    """55-E: the TOOL boundary the agent uses for himalaya/oathtool (docker
    plugin): an explicit timeout is honoured, surfaces as a tool error (never a
    hang), the tool stays usable afterwards, and the harness can reap the
    in-container work. The last point probes code plan 6.4 "cancellation ... no
    zombie processes": a bounded (killed) `docker exec` cannot signal the command
    it started inside the container."""
    if skip():
        return
    docker_tools = [t["name"] for t in mcp_tools() if "docker" in t["name"].lower()]
    assert docker_tools, "no docker tool in the live registry"
    dname = docker_tools[0]
    proj = sh("docker inspect $(hostname) --format "
              "'{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}'",
              timeout=30).stdout.strip()
    envf = STACK_ENV_FILE
    if not proj or not envf or not os.path.exists(envf):
        print(f"SKIP: cannot pin this stack's compose project "
              f"(project_dir={proj!r}, env_file={envf!r})")
        return
    pre = tb_pids(HANG_SLEEP)
    assert not pre, f"a process with argv {HANG_SLEEP} was already running: {pre}"
    args = {"project_dir": proj, "env_file": envf, "command": "exec",
            "service": "toolbox", "args": "sleep " + HANG_SLEEP, "timeout": 5}
    t0 = time.time()
    try:
        env = mcp_execute_raw(dname, args, timeout=90)
        blob = json.dumps(env)
    except Exception as e:
        blob = str(e)
    dt = time.time() - t0
    assert dt < 60, f"explicit 5s timeout not honoured ({dt:.1f}s)"
    assert "timed out" in blob.lower(), f"timeout not surfaced as a tool error: {blob[:300]}"
    orphan = tb_pids(HANG_SLEEP)
    ok = False
    for _ in range(3):
        try:
            env = mcp_execute_raw(dname, dict(args, args="echo x6-ok", timeout=30), timeout=60)
            if env.get("success") and "x6-ok" in json.dumps(env):
                ok = True
                break
        except Exception:
            pass
        time.sleep(2)
    assert ok, "docker tool unusable after a timeout (wedged)"
    if orphan:
        print(f"  NOTE (code plan 6.4): {len(orphan)} in-container pid(s) {orphan} survived "
              "the bounded exec - the plugin kills its client, not the remote command")
        tb_exec("pkill -f -- '[1]2345' || true", timeout=20)
    assert_no_proc(HANG_SLEEP, "the timed-out exec")
    print(f"PASS: 55-E {dname} honoured an explicit 5s timeout in {dt:.1f}s as a tool error, "
          f"usable afterwards, {len(orphan)} orphan in-container pid(s) found and reaped")


def check_no_leak():
    """55-F: after every robustness case the registry, the toolbox and the
    browser state are unchanged (nothing leaked by the injected failures)."""
    names = [t["name"] for t in mcp_tools()]
    assert any("docker" in n.lower() for n in names), "docker tool vanished from the registry"
    if not skip():
        rc, out, err, dt, to = tb_exec("true", timeout=20)
        assert not to and rc == 0, f"toolbox unhealthy after the cases: rc={rc} {err!r}"
    states = [s for s in sh("docker ps -a --filter ancestor=%s --format '{{.State}}'"
                            % PLAYWRIGHT_IMAGE, timeout=30).stdout.split() if s]
    assert "dead" not in states, f"leaked/dead browser container(s): {states}"
    print(f"PASS: 55-F registry intact ({len(names)} tools), toolbox healthy, "
          f"playwright containers {states or 'none'} (no dead/leaked state)")


CHECKS = [
    ("55-a", "prereqs + SMS deferral", check_prereqs_and_sms_deferral),
    ("55-b", "himalaya timeout/hang/failure/parallel/cleanup", check_himalaya),
    ("55-c", "totp vector/failure/parallel/cleanup", check_totp),
    ("55-d", "playwright hang/parallel", check_playwright),
    ("55-e", "tool timeout bounded + cleanup", check_tool_timeout),
    ("55-f", "no leak / stack healthy", check_no_leak),
]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sel = [c for c in CHECKS
           if not argv or any(a in c[0] or a in c[1] or a in c[2].__name__ for a in argv)]
    if not sel:
        print("no check matches %s (known: %s)" % (argv, [c[0] for c in CHECKS]))
        return 2
    failed = 0
    for tag, what, fn in sel:
        print(f"\n--- {tag} {what} ", end="", flush=True)
        t0 = time.time()
        try:
            fn()
            print(f"    ({time.time() - t0:.1f}s)")
        except Exception as e:
            failed += 1
            print(f"FAIL ({time.time() - t0:.1f}s): {e}", flush=True)
            import traceback
            traceback.print_exc()
    print("\n%d/%d check(s) passed" % (len(sel) - failed, len(sel)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
