"""Portable, foreground OpenSSH forwarding for a private Project Viz viewer."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


def _port(value, name, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 65535:
        raise ValueError(f"{name} must be an integer from {0 if allow_zero else 1} to 65535")
    return value


def _identity(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
        raise ValueError(f"Invalid {name}")
    return value


def _target(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("SSH target must be a host/config alias or user@host")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("SSH target cannot contain whitespace or control characters")
    if value.count("@") > 1:
        raise ValueError("SSH target must be a host/config alias or user@host")
    username, separator, host = value.rpartition("@")
    if not separator:
        host = value
    elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", username):
        raise ValueError("Invalid SSH username; use an SSH config alias for advanced settings")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            raise ValueError("Only IPv6 SSH hosts may use brackets")
    if ":" in host:
        try:
            if "%" in host:
                raise ValueError("Scoped addresses require an SSH config alias")
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("Invalid IPv6 SSH host; use an SSH config alias if needed") from exc
    elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}", host):
        raise ValueError("Invalid SSH host/config alias; do not include options or a port")
    return (username + "@" if separator else "") + host


def make_plan(descriptor, token, ssh_target, local_port=None, ssh_port=None):
    """Create a transferable plan; the access token makes this private data."""
    if not isinstance(descriptor, dict):
        raise ValueError("Viewer descriptor must be an object")
    project_id = _identity(descriptor.get("projectId"), "projectId")
    instance_id = _identity(descriptor.get("instanceId"), "instanceId")
    remote_port = _port(descriptor.get("port"), "Remote port")
    local_port = _port(remote_port if local_port is None else local_port, "Local port")
    if ssh_port is not None:
        ssh_port = _port(ssh_port, "SSH port")
    ssh_target = _target(ssh_target)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Invalid viewer access token")
    args = ["ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"]
    if ssh_port is not None:
        args.extend(["-p", str(ssh_port)])
    args.append(ssh_target)
    return {"schemaVersion": 1, "kind": "project-viz-ssh", "projectId": project_id,
            "instanceId": instance_id, "sshTarget": ssh_target, "sshPort": ssh_port,
            "remotePort": remote_port, "localPort": local_port,
            "browserUrl": f"http://127.0.0.1:{local_port}/?" + urllib.parse.urlencode({"token": token}),
            "sshArgs": args, "sshCommand": shlex.join(args),
            "powershellCommand": "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)}


def _validated_plan(plan):
    if not isinstance(plan, dict) or type(plan.get("schemaVersion")) is not int or plan.get("schemaVersion") != 1 or plan.get("kind") != "project-viz-ssh":
        raise ValueError("Expected a version 1 Project Viz SSH plan")
    local_port = _port(plan.get("localPort"), "Local port")
    url = plan.get("browserUrl")
    if not isinstance(url, str) or len(url) > 1024 or any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("Invalid private loopback browser URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        valid = (parsed.scheme == "http" and parsed.netloc == f"127.0.0.1:{local_port}"
                 and parsed.path == "/" and not parsed.fragment and set(query) == {"token"}
                 and len(query["token"]) == 1)
    except ValueError as exc:
        raise ValueError("Invalid private loopback browser URL") from exc
    if not valid:
        raise ValueError("Plan browser URL must match its local 127.0.0.1 port and contain only the access token")
    # Commands and argv in a transferred plan are display hints, never executable input.
    return make_plan({"projectId": plan.get("projectId"), "instanceId": plan.get("instanceId"),
                      "port": plan.get("remotePort")}, query["token"][0],
                     plan.get("sshTarget"), local_port, plan.get("sshPort"))


def _available_port(requested):
    """Check before launching; OpenSSH also rejects a bind race with ExitOnForwardFailure."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", requested))
        except OSError as exc:
            raise RuntimeError(f"Local port {requested} is unavailable; choose --local-port 0 or another port") from exc
        return probe.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _health(plan, timeout):
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(plan["browserUrl"]).query)["token"][0]
    request = urllib.request.Request(f'http://127.0.0.1:{plan["localPort"]}/api/health',
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(16385)
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            return False
        raise RuntimeError(f"Forwarded viewer rejected the health check (HTTP {exc.code}); refresh the remote plan") from exc
    except (OSError, urllib.error.URLError):
        return False
    try:
        if len(raw) > 16384:
            raise ValueError("Oversized response")
        health = json.loads(raw)
        valid = isinstance(health, dict) and all(health.get(key) == plan[key] for key in ("projectId", "instanceId"))
    except (ValueError, UnicodeDecodeError):
        valid = False
    if not valid:
        raise RuntimeError("Forwarded viewer identity does not match the plan; refresh the remote plan")
    return True


def _stop_child(child):
    """Only touch the process created by this invocation, including error paths."""
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    else:
        child.wait()


def run_tunnel(plan, local_port=None, open_browser=False, timeout=30):
    """Verify a tunnel, print a ready JSON line, and hold it until exit or Ctrl+C.

    Run this on the browser's computer, not the server. OpenSSH inherits the
    terminal for host-key/password prompts. SSH config handles keys and jumps.
    """
    validated = _validated_plan(plan)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError("Tunnel timeout must be a finite number greater than 0 and at most 600 seconds")
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("OpenSSH client was not found; install the OpenSSH Client optional feature on Windows, or your system's openssh-client package")
    requested = validated["localPort"] if local_port is None else _port(local_port, "Local port", allow_zero=True)
    chosen = _available_port(requested)
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(validated["browserUrl"]).query)["token"][0]
    validated = make_plan({"projectId": validated["projectId"], "instanceId": validated["instanceId"],
                           "port": validated["remotePort"]}, token, validated["sshTarget"], chosen, validated["sshPort"])
    args = [ssh, *validated["sshArgs"][1:]]
    try:
        child = subprocess.Popen(args, shell=False)
    except OSError as exc:
        raise RuntimeError("Could not launch the OpenSSH client") from exc
    try:
        deadline = time.monotonic() + timeout
        while True:
            code = child.poll()
            if code is not None:
                raise RuntimeError(f"SSH exited before the viewer connected (exit {code}); check the SSH messages above")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("SSH viewer connection timed out; finish SSH authorization or check the remote viewer and generate a fresh plan")
            if _health(validated, timeout=min(1.5, remaining)):
                break
            time.sleep(min(.2, remaining))
        result = {"connected": True, "projectId": validated["projectId"], "instanceId": validated["instanceId"],
                  "localPort": chosen, "remotePort": validated["remotePort"],
                  "url": validated["browserUrl"], "browserUrl": validated["browserUrl"]}
        if open_browser:
            try:
                result["browserOpened"] = bool(webbrowser.open(validated["browserUrl"]))
            except (OSError, webbrowser.Error):
                result["browserOpened"] = False
        print(json.dumps(result, ensure_ascii=False), flush=True)
        code = child.wait()
        if code:
            raise RuntimeError(f"SSH tunnel closed with exit {code}; rerun the tunnel command to reconnect")
        return {"connected": False, "stopped": True, "exitCode": code}
    except KeyboardInterrupt:
        return {"connected": False, "stopped": True, "interrupted": True}
    finally:
        _stop_child(child)
