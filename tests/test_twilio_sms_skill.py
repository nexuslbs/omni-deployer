#!/usr/bin/env python3
"""Unit test for the ``sms-read-twilio`` skill helper.

The skill ``profiles/omni/skills/communication/sms-read-twilio`` (omni-root,
profile content shared by ``main`` + ``dev``) ships ``twilio_sms.py``: the
on-demand Twilio REST reader used to read SMS / extract a verification code.
The live API path is credential- and network-bound, but the code-extraction and
message-selection logic is pure and must stay deterministic - that is what this
script checks. No network, no credentials, stdlib only.

    python3 tests/test_twilio_sms_skill.py

Overrides: ``TWILIO_SKILL_DIR`` (directory holding ``twilio_sms.py``). Without a
checkout the script prints SKIP and exits 0, so it can live in the shared test
suite of a workspace that does not mount omni-root.

Exit code 0 = pass (or skip), 1 = behaviour regression.
Added by the tester of task_omnidev_hermes_data_sync_from_s3_twilio_sms.
"""

import importlib.util
import os
import re
import sys

CANDIDATES = [
    os.environ.get("TWILIO_SKILL_DIR"),
    "/opt/omni/profiles/omni/skills/communication/sms-read-twilio",
    "/opt/workspace/omni-root/profiles/omni/skills/communication/sms-read-twilio",
]

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))


def load_helper():
    for d in CANDIDATES:
        if not d:
            continue
        path = os.path.join(d, "twilio_sms.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("twilio_sms_under_test", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, path
    return None, None


def msg(sid, body, direction="inbound", status="received"):
    return {
        "sid": sid,
        "direction": direction,
        "status": status,
        "error_code": None,
        "body": body,
        "from": "+18330000000",
        "to": "+16562067498",
    }


def main():
    m, path = load_helper()
    if m is None:
        print("SKIP: twilio_sms.py not found in: %s" % ", ".join(c for c in CANDIDATES if c))
        return 0
    print("helper under test: %s" % path)

    # --- code extraction: standalone 4-8 digit code, never part of a longer number
    check("pick_code: 6-digit code in OTP wording", m.pick_code("Your code is 481920 thanks") == "481920")
    check("pick_code: 4-digit pin", m.pick_code("PIN 1234") == "1234")
    check("pick_code: rejects 9-digit reference", m.pick_code("ref 123456789") is None)
    check("pick_code: rejects digits embedded in a 16-digit card", m.pick_code("card 4111111111111111") is None)
    check("pick_code: no digits -> None", m.pick_code("hello there") is None)

    # --- classification (otp / service / plain)
    check("classify: OTP wording", m.classify(msg("SM1", "Your verification code is 90210")) == "otp")
    check(
        "classify: marketing/bulk sender",
        m.classify(msg("SM2", "BulkSMS.com covers over 1200 networks worldwide! Reply STOP")) == "service",
    )
    check("classify: plain", m.classify(msg("SM3", "Package delivered at 15:30")) == "plain")

    # --- selection: newest first, inbound only, prefer OTP-with-code, skip bulk
    page = [
        msg("SMa", "BulkSMS.com covers over 1200 networks worldwide! Reply STOP"),
        msg("SMb", "Your OTP code is 55221"),
        msg("SMc", "Your OTP code is 11987", status="failed"),
        msg("SMd", "hi", direction="outbound-api"),
    ]
    chosen, confidence = m.choose_message(page, want_code=True)
    check(
        "choose_message: picks the newest inbound OTP message with a code",
        chosen is not None and chosen["sid"] == "SMb",
        "chosen=%s" % (chosen and chosen["sid"]),
    )
    check("choose_message: high confidence for OTP+code", bool(confidence) and confidence.startswith("high"), confidence)
    check("choose_message: never returns an outbound echo", chosen is not None and m.is_inbound(chosen))

    none_chosen, none_conf = m.choose_message([msg("SMz", "x", direction="outbound-api")], want_code=True)
    check("choose_message: no inbound -> (None, None)", none_chosen is None and none_conf is None)

    no_code, low_conf = m.choose_message([msg("SMy", "your parcel arrived")], want_code=True)
    check(
        "choose_message: inbound without a code -> LOW confidence, never silent",
        no_code is not None and bool(low_conf) and low_conf.startswith("LOW"),
        low_conf,
    )

    # --- is_inbound helper used by the selection
    check("is_inbound: inbound", m.is_inbound(msg("SM1", "x")))
    check("is_inbound: outbound-api", not m.is_inbound(msg("SM1", "x", direction="outbound-api")))

    # --- secret hygiene: the shipped helper must never carry a literal credential
    src = open(path, encoding="utf-8", errors="replace").read()
    check("helper source: no literal Account SID (AC+32hex)", re.search(r"AC[0-9a-f]{32}", src) is None)
    check("helper source: no literal 32-hex auth token", re.search(r"\b[0-9a-f]{32}\b", src) is None)

    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        line = "%s %s" % ("ok  " if ok else "FAIL", name)
        if not ok and detail:
            line += " (%s)" % detail
        print(line)
    print("\n%d checks, %d failed" % (len(CHECKS), len(failed)))
    if failed:
        print("FAIL: sms-read-twilio skill helper behaviour regression")
        return 1
    print("PASS: sms-read-twilio skill helper behaviour holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
