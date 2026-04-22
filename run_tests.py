"""Run the scenario suite we use to sanity-check the ROM + host driver.

Builds the ROM, then for each scenario launches a fresh BGB, runs the
matching `gbforth.py` subcommand, captures a BMP where applicable, and
reports pass/fail. Exits non-zero if anything failed.

Usage:
  python run_tests.py                  # build, then run every scenario
  python run_tests.py selftest hello   # just the named ones
  python run_tests.py --no-build       # skip rgbasm/rgblink/rgbfix
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = "build"
SHOT_DIR = "screenshots"
PORT = 8765
BGB = "bgb64"
ROM = "3forth.gb"


def sh(*cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def build():
    os.makedirs(BUILD_DIR, exist_ok=True)
    obj = os.path.join(BUILD_DIR, "3forth.o")
    sym = os.path.join(BUILD_DIR, "3forth.sym")
    sh("rgbasm",  "-o", obj, "3forth.asm")
    sh("rgblink", "-o", ROM, "-n", sym, obj)
    sh("rgbfix",  "-p", "0", "-v", ROM)


def port_free(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        return True


def wait_for_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"BGB didn't start listening on :{port} within {timeout}s")


def start_bgb(bmp_path=None):
    args = [BGB, "-rom", ROM, "-listen", f"127.0.0.1:{PORT}",
            "-nowarn", "-nowriteini"]
    if bmp_path:
        args += ["-br", "8", "-autoexit", "-screenonexit", bmp_path]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_port(PORT)
    except Exception:
        proc.terminate()
        raise
    return proc


def stop_bgb(proc, autoexit):
    # With -autoexit, BGB tears itself down after the $0008 breakpoint fires;
    # wait for that so the BMP is fully written before we check for it.
    if autoexit and proc.poll() is None:
        try:
            proc.wait(5)
            return
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2)


def check(name, rc, out, bmp_path):
    if rc != 0:
        return f"gbforth exited {rc}"
    if name == "selftest":
        m = re.search(r"(\d+)\s+failure", out)
        if not m:
            return "no failure count in output"
        if int(m.group(1)) != 0:
            return f"{m.group(1)} failure(s)"
        m2 = re.search(r"(\d+)/(\d+)\s+mismatches", out)
        if m2 and int(m2.group(1)) != 0:
            return f"{m2.group(1)}/{m2.group(2)} WRAM mismatches"
    if bmp_path is not None:
        if not os.path.exists(bmp_path):
            return "no BMP produced"
        size = os.path.getsize(bmp_path)
        if size < 1000:
            return f"BMP only {size} bytes"
    return None


TESTS = [
    # (name, gbforth args, halt?, expected-BMP filename)
    ("selftest", ["selftest"],                                 False, None),
    ("hello",    ["hello", "--halt"],                          True,  "hello.bmp"),
    ("print",    ["print", "HELLO WORLD", "--halt"],           True,  "print.bmp"),
    ("scroll",   ["scroll", "HI", "--cycles", "1", "--halt"],  True,  "scroll.bmp"),
]


def run_one(name, args, halt, bmp):
    bmp_path = os.path.join(HERE, SHOT_DIR, bmp) if bmp else None
    if bmp_path:
        os.makedirs(os.path.dirname(bmp_path), exist_ok=True)
        if os.path.exists(bmp_path):
            os.remove(bmp_path)

    bgb = start_bgb(bmp_path if halt else None)
    t0 = time.time()
    try:
        cmd = [sys.executable, "gbforth.py", *args]
        print(f"\n=== {name}: {' '.join(cmd)} ===", flush=True)
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        sys.stdout.write(cp.stdout)
        if cp.stderr:
            sys.stderr.write(cp.stderr)
    finally:
        stop_bgb(bgb, autoexit=halt)
    return cp.returncode, cp.stdout, bmp_path, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*",
                    help=f"scenarios to run (default: all of {[t[0] for t in TESTS]})")
    ap.add_argument("--no-build", action="store_true",
                    help="skip rgbasm/rgblink/rgbfix")
    ap.add_argument("--no-png", action="store_true",
                    help="skip bmp2png conversion at the end")
    args = ap.parse_args()

    os.chdir(HERE)

    if not port_free(PORT):
        raise SystemExit(f"port {PORT} already in use — is BGB already running?")

    if not args.no_build:
        build()

    selected = TESTS if not args.names else [t for t in TESTS if t[0] in args.names]
    if not selected:
        known = ", ".join(t[0] for t in TESTS)
        raise SystemExit(f"no matching scenarios in [{known}]: {args.names}")

    results = []
    for name, sub_args, halt, bmp in selected:
        rc, out, bmp_path, dt = run_one(name, sub_args, halt, bmp)
        results.append((name, rc, out, bmp_path, dt))

    if not args.no_png:
        for _, _, _, bmp_path, _ in results:
            if bmp_path and os.path.exists(bmp_path):
                png_path = os.path.splitext(bmp_path)[0] + ".png"
                sh(sys.executable, "bmp2png.py", bmp_path, png_path)

    print("\n=== summary ===")
    failures = 0
    for name, rc, out, bmp_path, dt in results:
        err = check(name, rc, out, bmp_path)
        tag = "PASS" if err is None else f"FAIL ({err})"
        print(f"  {tag:30s}  {name:10s}  {dt:5.1f}s")
        if err:
            failures += 1
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
