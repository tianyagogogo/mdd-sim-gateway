#!/usr/bin/env python3
"""Run the real country SOCKS service in a disposable, offline Docker laboratory.

Three containers, two internal networks, no host ports or NET_ADMIN. The client
cannot reach the UDP peer directly; two SOCKS hops provide the only working path.
Requires local PySocks, a verified Linux amd64 sing-box binary and an existing NAS
image containing python3 and PyYAML. Does not pull images or touch business data.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time
import uuid

import socks

ROOT = Path(__file__).resolve().parents[2]

PEER = r'''
import json, socket, subprocess, threading
from pathlib import Path
config = {"log": {"level": "error"},
          "inbounds": [{"type": "socks", "listen": "0.0.0.0", "listen_port": 1080}],
          "outbounds": [{"type": "direct"}]}
Path("/tmp/peer.json").write_text(json.dumps(config))
process = subprocess.Popen(["/probe/sing-box", "run", "-c", "/tmp/peer.json"])
Path("/tmp/upstream.pid").write_text(str(process.pid))
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 15000))
sock.settimeout(1)
print("READY", flush=True)
while True:
    try:
        payload, address = sock.recvfrom(65535)
    except socket.timeout:
        continue
    sock.sendto(payload, address)
'''

START_EGRESS = r'''
import json, os
from pathlib import Path
root = Path("/data/orchestrator")
root.mkdir(parents=True, exist_ok=True)
desired = {"proxy": {"enabled": True, "exits": {
    "gb": {"enabled": True, "mode": "manual",
           "proxy_url": "socks5://" + os.environ["PEER_IP"] + ":1080"}}}}
(root / "desired.json").write_text(json.dumps(desired))
os.execvp("python3", ["python3", "-m", "runtime.egress", "--data", "/data", "--interval", "0.5"])
'''

CLIENT = r'''
import json, multiprocessing, os, select, socket, sys, time
from pathlib import Path
from engine.outer_transport import proxy_udp_socket
target = (os.environ["PEER_IP"], 15000)
proxy = "socks5://mdd-egress:22157"
mode = sys.argv[1]
def blocked(sock):
    try:
        sock.sendto(b"must-not-arrive", target)
        sock.recvfrom(4096)
    except OSError:
        return True
    return False
if mode == "direct-blocked":
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        assert blocked(sock), "client bypassed isolated egress"
elif mode == "blocked":
    try:
        sock = proxy_udp_socket(proxy, target, timeout=1)
    except OSError:
        pass
    else:
        with sock:
            assert blocked(sock), "proxy unavailable but traffic succeeded"
elif mode == "hold":
    with proxy_udp_socket(proxy, target, timeout=1) as sock:
        sock.sendto(b"before-stop", target)
        assert sock.recvfrom(4096)[0] == b"before-stop"
        Path("/tmp/held-ready").touch()
        deadline = time.monotonic() + 30
        while not Path("/tmp/check-held").exists():
            assert time.monotonic() < deadline, "no failure trigger"
            time.sleep(0.1)
        assert blocked(sock), "established association leaked after stop"
        Path("/tmp/held-result").write_text("blocked")
elif mode == "roundtrip":
    # Two independent associations model IKE/500 and NAT-T/4500. Echo tests are
    # synthetic and do not prove an operator's IKE/NAT traversal behaviour.
    with proxy_udp_socket(proxy, target, timeout=2) as ike, \
         proxy_udp_socket(proxy, target, timeout=2) as natt:
        for size in (1, 500, 1200, 1400):
            payloads = [(ike, b"i" * size), (natt, b"n" * size)]
            for sock, payload in payloads:
                sock.sendto(payload, target)
            for sock, payload in payloads:
                assert select.select([sock], [], [], 2)[0], "select did not wake"
                response, origin = sock.recvfrom(4096)
                assert response == payload and origin == target
        child = multiprocessing.get_context("fork").Process(
            target=ike.sendto, args=(b"fork-worker", target))
        child.start()
        assert ike.recvfrom(4096)[0] == b"fork-worker"
        child.join(3)
        assert child.exitcode == 0
else:
    raise ValueError("unknown probe mode")
print(json.dumps({"mode": mode, "passed": True}), flush=True)
'''

SWU_CLIENT = r'''
import json, os, select, socket, sys
sys.path.insert(0, "/usr/local/bin")
from swu_ike import swu, UDP

target = (os.environ["PEER_IP"], 15000)
app = swu.__new__(swu)
app.socket_type = UDP
app.timeout = 2
app.egress_proxy = "socks5://mdd-egress:22157"
app.proxy_udp_overhead = 10
app.server_address = target
app.server_address_nat = target
app.create_socket(("0.0.0.0", 0))
app.create_socket_nat(("0.0.0.0", 0))
app.create_socket_esp(("0.0.0.0", 0))
try:
    assert app.socket_esp.type == socket.SOCK_DGRAM, "proxy mode opened raw ESP"
    for label, transport in ((b"ike", app.socket), (b"natt", app.socket_nat)):
        transport.sendto(label, target)
        assert select.select([transport], [], [], 2)[0], "SWu transport did not wake"
        response, origin = transport.recvfrom(4096)
        assert response == label and origin == target
finally:
    app.socket.close()
    app.socket_nat.close()
    app.socket_esp.close()
print(json.dumps({"mode": "engine-swu-roundtrip", "passed": True}), flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--client-image",
                        help="optional Engine image whose installed SWu transport is exercised")
    parser.add_argument("--singbox", type=Path, required=True)
    args = parser.parse_args()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]

    def remote(argv, *, data=None, check=True, combined=False):
        result = subprocess.run(ssh + [shlex.join(argv)], input=data,
                                capture_output=True, timeout=60)
        if check and result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace") or result.stdout.decode())
        output = result.stdout + (result.stderr if combined else b"")
        return output.decode(errors="replace").strip()

    def docker(*argv, check=True):
        return remote(["sudo", "-n", "/usr/local/bin/docker", *argv], check=check,
                      combined=bool(argv and argv[0] == "logs"))

    def snapshot():
        result = {}
        for family in ("-4", "-6"):
            for command in ("route", "rule"):
                args = ["ip", family, command, "show"]
                if command == "route":
                    args += ["table", "all"]
                raw = remote(args)
                result[family + command] = sorted(re.sub(
                    r"\bexpires \d+sec\b", "expires <lifetime>", raw).splitlines())
        return result

    def wait_for(function, description):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if function():
                return
            time.sleep(0.3)
        raise RuntimeError("timed out: " + description)

    image = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    prefix = "mdd-udp-proof-" + uuid.uuid4().hex[:10]
    front, back = prefix + "-front", prefix + "-back"
    peer, egress, client = prefix + "-peer", prefix + "-egress", prefix + "-client"
    engine_client = prefix + "-engine"
    before = snapshot()
    stage = remote(["mktemp", "-d", "/tmp/mdd-udp-proof.XXXXXX"])
    if not re.fullmatch(r"/tmp/mdd-udp-proof\.[A-Za-z0-9]+", stage):
        raise RuntimeError("unexpected staging directory")
    checks = []
    cleanup_errors = []
    try:
        payload = io.BytesIO()
        files = {"peer.py": PEER.encode(), "start_egress.py": START_EGRESS.encode(),
                 "client.py": CLIENT.encode(), "swu_client.py": SWU_CLIENT.encode(),
                 "sing-box": args.singbox.read_bytes(),
                 "vendor/socks.py": Path(socks.__file__).read_bytes()}
        for path in ("runtime/egress.py", "host/mdd_orchestrator.py", "engine/outer_transport.py",
                     "control/app/egress_contract.py"):
            files[path] = (ROOT / path).read_bytes()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o755 if name == "sing-box" else 0o644
                archive.addfile(member, io.BytesIO(data))
        remote(["tar", "xf", "-", "-C", stage], data=payload.getvalue())
        # Container root has no DAC_OVERRIDE; the code-only staging directory is
        # owned by the SSH user. Make it traversable without adding capabilities.
        remote(["chmod", "755", stage])
        for network in (front, back):
            docker("network", "create", "--internal", "--label",
                   "io.mdd-sim-gateway.component=udp-proof", network)

        def start(name, network, program, *extra):
            docker("run", "--detach", "--name", name, "--network", network,
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", "192m", "--pids-limit", "64",
                   "--label", "com.centurylinklabs.watchtower.enable=false",
                   "--label", "io.mdd-sim-gateway.component=udp-proof",
                   "--mount", "type=bind,src=" + stage + ",dst=/probe,readonly",
                   "--tmpfs", "/tmp:rw,size=16m", "--tmpfs", "/data:rw,size=16m",
                   "--workdir", "/probe", "--env", "PYTHONPATH=/probe:/probe/vendor",
                   "--env", "MDD_SINGBOX_BIN=/probe/sing-box", *extra,
                   "--entrypoint", "python3", image, *program)

        start(peer, back, ["peer.py"])
        wait_for(lambda: "READY" in docker("logs", peer), "UDP peer")
        peer_ip = docker("inspect", peer, "--format",
                         '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
        start(egress, back, ["start_egress.py"], "--env", "PEER_IP=" + peer_ip)
        docker("network", "connect", "--alias", "mdd-egress", front, egress)
        start(client, front, ["-c", "import time; time.sleep(300)"], "--env", "PEER_IP=" + peer_ip)

        def ready():
            raw = docker("exec", egress, "cat", "/data/orchestrator/socks-egress-status.json", check=False)
            try:
                return bool(json.loads(raw).get("exits", {}).get("gb", {}).get("ready"))
            except ValueError:
                return False

        def check(mode):
            output = docker("exec", client, "python3", "client.py", mode)
            checks.append(json.loads(output))

        wait_for(ready, "country SOCKS readiness")
        check("direct-blocked")
        check("roundtrip")
        if args.client_image:
            docker("image", "inspect", args.client_image)
            docker("run", "--detach", "--name", engine_client, "--network", front,
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", "256m", "--pids-limit", "64",
                   "--label", "io.mdd-sim-gateway.component=udp-proof",
                   "--mount", "type=bind,src=" + stage + ",dst=/probe,readonly",
                   "--tmpfs", "/tmp:rw,size=16m", "--workdir", "/probe",
                   "--env", "PEER_IP=" + peer_ip, "--entrypoint", "python3",
                   args.client_image, "-c", "import time; time.sleep(300)")
            output = docker("exec", engine_client, "python3", "swu_client.py")
            checks.append(json.loads(output))
        docker("exec", "--detach", client, "python3", "client.py", "hold")
        wait_for(lambda: docker("exec", client, "ls", "/tmp/held-ready", check=False), "live association")
        docker("stop", "--time", "5", egress)
        docker("exec", client, "touch", "/tmp/check-held")
        check("blocked")
        wait_for(lambda: docker("exec", client, "cat", "/tmp/held-result", check=False) == "blocked",
                 "existing association fail-closed")
        checks.append({"mode": "established-association-blocked", "passed": True})
        docker("start", egress)
        wait_for(ready, "country SOCKS recovery")
        check("roundtrip")
        # Kill only the upstream relay, leaving its UDP target alive. Egress can
        # reach the target directly, so a silent direct fallback would be caught.
        docker("exec", peer, "python3", "-c",
               'import os,signal; os.kill(int(open("/tmp/upstream.pid").read()), signal.SIGTERM)')
        docker("exec", egress, "python3", "-c",
               'import os,socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); '
               's.settimeout(2); s.sendto(b"target-alive",(os.environ["PEER_IP"],15000)); '
               'assert s.recvfrom(100)[0]==b"target-alive"')
        check("blocked")
        checks[-1]["mode"] = "upstream-failure-no-direct-fallback"
        docker("exec", "--detach", peer, "/probe/sing-box", "run", "-c", "/tmp/peer.json")
        wait_for(lambda: docker("exec", egress, "python3", "-c",
                 'import os,socket; s=socket.create_connection((os.environ["PEER_IP"],1080),1); '
                 's.close(); print("ready")', check=False) == "ready", "upstream recovery")
        check("roundtrip")
        check("direct-blocked")
    except BaseException:
        # Component logs are from synthetic fixtures, with no subscriber data or credentials.
        for name in (peer, egress, client, engine_client):
            print(name, docker("logs", name, check=False))
        raise
    finally:
        for name in (engine_client, client, egress, peer):
            docker("rm", "--force", name, check=False)
        for network in (front, back):
            docker("network", "rm", network, check=False)
        remaining = docker("ps", "--all", "--quiet", "--filter", "name=" + prefix)
        networks = docker("network", "ls", "--quiet", "--filter", "name=" + prefix)
        if remaining or networks:
            cleanup_errors.append("laboratory containers or networks remain")
        # This directory was created by mktemp in this invocation and validated above.
        remote(["rm", "-rf", "--", stage])
    after = snapshot()
    report = {"checks": checks, "host_routes_rules_restored": before == after,
              "cleanup_errors": cleanup_errors, "image_id": image,
              "singbox_sha256": hashlib.sha256(args.singbox.read_bytes()).hexdigest()}
    print(json.dumps(report, indent=2))
    if before != after or cleanup_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
