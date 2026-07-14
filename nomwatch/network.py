"""Explicit private-interface LAN binding, staged confirmation, and mDNS."""
from __future__ import annotations

import ipaddress
import json
import platform
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, Optional

from waitress import create_server

from .state import LocalState, StateError

REJECTED_INTERFACE_PREFIXES = ("lo", "utun", "tun", "tap", "tailscale", "docker", "veth", "br-")
IP_BOUND_IF = 25
IPV6_BOUND_IF = 125


class NetworkPolicyError(StateError):
    pass


@dataclass(frozen=True)
class InterfaceAddress:
    interface: str
    index: int
    address: str
    prefix_length: int

    @property
    def network(self):
        return ipaddress.ip_interface(f"{self.address}/{self.prefix_length}").network


def is_allowed_private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in ipaddress.ip_network("10.0.0.0/8") or address in ipaddress.ip_network("172.16.0.0/12") or address in ipaddress.ip_network("192.168.0.0/16")
    return address in ipaddress.ip_network("fc00::/7")


def enumerate_interfaces(runner=subprocess.run, system: Optional[str] = None) -> list[InterfaceAddress]:
    system = system or platform.system()
    indexes = dict(socket.if_nameindex())
    found: list[InterfaceAddress] = []
    if system == "Linux":
        result = runner(["ip", "-j", "address", "show"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            raise NetworkPolicyError("could not enumerate Linux interfaces")
        for item in json.loads(result.stdout):
            name = item.get("ifname", "")
            for info in item.get("addr_info", []):
                address = info.get("local")
                if address and is_allowed_private_address(address):
                    found.append(InterfaceAddress(name, indexes.get(name, item.get("ifindex", 0)), address, int(info["prefixlen"])))
    elif system == "Darwin":
        result = runner(["ifconfig"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            raise NetworkPolicyError("could not enumerate macOS interfaces")
        current = None
        for line in result.stdout.splitlines():
            if line and not line[0].isspace():
                current = line.split(":", 1)[0]
            if not current:
                continue
            ipv4 = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask 0x([0-9a-fA-F]+)", line)
            ipv6 = re.search(r"\binet6 ([0-9a-fA-F:]+)(?:%\S+)? prefixlen (\d+)", line)
            if ipv4 and is_allowed_private_address(ipv4.group(1)):
                prefix = bin(int(ipv4.group(2), 16)).count("1")
                found.append(InterfaceAddress(current, indexes.get(current, 0), ipv4.group(1), prefix))
            elif ipv6 and is_allowed_private_address(ipv6.group(1)):
                found.append(InterfaceAddress(current, indexes.get(current, 0), ipv6.group(1), int(ipv6.group(2))))
    else:
        raise NetworkPolicyError(f"unsupported LAN host platform: {system}")
    return [item for item in found if not item.interface.lower().startswith(REJECTED_INTERFACE_PREFIXES)]


def create_interface_bound_socket(candidate: InterfaceAddress, port: int,
                                  *, system: Optional[str] = None, socket_factory=socket.socket):
    if not is_allowed_private_address(candidate.address):
        raise NetworkPolicyError("LAN address must be a directly selected private address")
    system = system or platform.system()
    family = socket.AF_INET6 if ":" in candidate.address else socket.AF_INET
    sock = socket_factory(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if system == "Darwin":
            level = socket.IPPROTO_IPV6 if family == socket.AF_INET6 else socket.IPPROTO_IP
            option = IPV6_BOUND_IF if family == socket.AF_INET6 else IP_BOUND_IF
            sock.setsockopt(level, option, candidate.index)
        elif system == "Linux":
            option = getattr(socket, "SO_BINDTODEVICE", 25)
            sock.setsockopt(socket.SOL_SOCKET, option, candidate.interface.encode() + b"\0")
        else:
            raise NetworkPolicyError("interface-bound sockets are unsupported on this platform")
        bind_address = (candidate.address, port, 0, candidate.index) if family == socket.AF_INET6 else (candidate.address, port)
        sock.bind(bind_address)
        sock.listen(128)
        return sock
    except (OSError, NetworkPolicyError) as exc:
        sock.close()
        raise NetworkPolicyError(f"could not enforce ingress isolation on {candidate.interface}: {exc}") from exc


class MdnsAdvertiser:
    def __init__(self, *, zeroconf_factory=None):
        if zeroconf_factory is None:
            from zeroconf import Zeroconf
            zeroconf_factory = Zeroconf
        self.zeroconf_factory = zeroconf_factory
        self.zeroconf = None
        self.info = None

    def start(self, hostname: str, address: str, port: int, display_suffix: str) -> None:
        from zeroconf import ServiceInfo
        packed = ipaddress.ip_address(address).packed
        self.zeroconf = self.zeroconf_factory()
        self.info = ServiceInfo(
            "_nomwatch._tcp.local.", f"NomWatch {display_suffix}._nomwatch._tcp.local.",
            addresses=[packed], port=port, properties={b"txtvers": b"1", b"protovers": b"1"},
            server=f"{hostname}.",
        )
        self.zeroconf.register_service(self.info, allow_name_change=True)

    def stop(self) -> None:
        if self.zeroconf and self.info:
            self.zeroconf.unregister_service(self.info)
        if self.zeroconf:
            self.zeroconf.close()
        self.zeroconf = self.info = None


class LanAccessManager:
    def __init__(self, state: LocalState, port: int, *, interface_provider=enumerate_interfaces,
                 socket_binder=create_interface_bound_socket, clock=time.time,
                 advertiser: Optional[MdnsAdvertiser] = None):
        self.state = state
        self.port = port
        self.interface_provider = interface_provider
        self.socket_binder = socket_binder
        self.clock = clock
        self.advertiser = advertiser
        self.app = None
        self.server = None
        self.thread = None
        self.pending = None
        self.confirmed: Optional[InterfaceAddress] = None
        self.error: Optional[str] = None

    def attach_app(self, app) -> None:
        self.app = app

    def interfaces(self) -> list[dict]:
        return [asdict(item) for item in self.interface_provider()]

    def allowed_hosts(self) -> set[str]:
        hosts = {"localhost", "127.0.0.1", "::1"}
        if self.pending:
            hosts.add(self.pending["candidate"].address.lower())
        if self.confirmed:
            hosts.add(self.confirmed.address.lower())
            with self.state.connect() as conn:
                row = conn.execute("SELECT mdns_slug FROM installation LIMIT 1").fetchone()
            hosts.add(f"{row[0]}.local")
        return hosts

    def stage(self, interface: str, address: str) -> dict:
        if self.app is None:
            raise NetworkPolicyError("gateway is not ready")
        matches = [item for item in self.interface_provider() if item.interface == interface and item.address == address]
        if len(matches) != 1:
            raise NetworkPolicyError("selected address is not currently assigned to that interface")
        self._stop_listener()
        candidate = matches[0]
        self._start_listener(candidate)
        token = secrets.token_urlsafe(24)
        self.pending = {"candidate": candidate, "token": token, "deadline": self.clock() + 60}
        return {"confirmation_token": token, "expires_in_seconds": 60,
                "candidate_url": f"http://{address}:{self.port}/access"}

    def _start_listener(self, candidate: InterfaceAddress) -> None:
        sock = self.socket_binder(candidate, self.port)
        self.server = create_server(self.app, sockets=[sock], threads=4)
        self.thread = threading.Thread(target=self.server.run, name="nomwatch-lan", daemon=True)
        self.thread.start()
        self.error = None

    def restore(self) -> bool:
        """Restore only the exact persisted interface/address; any drift fails closed."""
        with self.state.connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE namespace='network'").fetchone()
        if not row:
            return False
        value = json.loads(row[0])
        if not value.get("lan_enabled"):
            return False
        matches = [item for item in self.interface_provider()
                   if item.interface == value.get("interface") and item.address == value.get("address")]
        if len(matches) != 1:
            self.error = "selected LAN address is no longer assigned; listener remains disabled"
            return False
        try:
            self._start_listener(matches[0])
        except NetworkPolicyError as exc:
            self.error = str(exc)
            return False
        self.confirmed = matches[0]
        self._advertise(matches[0])
        return True

    def reconcile(self) -> bool:
        if not self.confirmed:
            return False
        current = {(item.interface, item.address) for item in self.interface_provider()}
        if (self.confirmed.interface, self.confirmed.address) not in current:
            self.error = "LAN interface changed; listener closed to prevent cross-interface exposure"
            self._stop_listener()
            self.confirmed = None
            return False
        return True

    def confirm(self, token: str, observed_host: str) -> dict:
        pending = self.pending
        if not pending or pending["deadline"] < self.clock() or not secrets.compare_digest(pending["token"], token):
            self._stop_listener()
            self.pending = None
            raise NetworkPolicyError("LAN stage expired or confirmation token is invalid")
        candidate = pending["candidate"]
        raw = observed_host.lower()
        observed = raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw else raw.split(":", 1)[0]
        if observed != candidate.address:
            raise NetworkPolicyError("confirm LAN access through the staged address")
        self.confirmed = candidate
        self.pending = None
        self.state.put_setting("network", {"lan_enabled": True, **asdict(candidate)})
        self._advertise(candidate)
        return self.status()

    def _advertise(self, candidate: InterfaceAddress) -> None:
        if self.advertiser:
            with self.state.connect() as conn:
                slug = conn.execute("SELECT mdns_slug FROM installation LIMIT 1").fetchone()[0]
            self.advertiser.start(f"{slug}.local", candidate.address, self.port, slug[-4:].upper())

    def disable(self) -> None:
        self._stop_listener()
        self.confirmed = self.pending = None
        self.state.put_setting("network", {"lan_enabled": False})
        if self.advertiser:
            self.advertiser.stop()

    def shutdown(self) -> None:
        """Stop runtime listeners without changing the persisted desired state."""
        self._stop_listener()
        if self.advertiser:
            self.advertiser.stop()

    def _stop_listener(self) -> None:
        if self.server:
            self.server.close()
        self.server = None
        self.thread = None

    def status(self) -> dict:
        if self.pending and self.pending["deadline"] < self.clock():
            self._stop_listener()
            self.pending = None
        candidate = self.confirmed
        return {
            "enabled": candidate is not None,
            "interface": candidate.interface if candidate else None,
            "address": candidate.address if candidate else None,
            "mdns_url": next((f"http://{host}:{self.port}/" for host in self.allowed_hosts() if host.endswith(".local")), None),
            "ip_url": f"http://{candidate.address}:{self.port}/" if candidate else None,
            "trusted_lan_warning": "LAN HTTP is not encrypted. Devices or attackers on this network may observe credentials, sessions, and video. Prefer Tailscale HTTPS.",
            "error": self.error,
        }


BLOCKED_CAMERA_PORTS = {22, 25, 53, 80, 111, 443, 2375, 2376, 3306, 5432, 6379, 6443, 8080, 8500, 9200, 11211}


def validate_camera_target(host: str, port: int, allowed_networks=(), resolver=socket.getaddrinfo) -> str:
    if port < 1 or port > 65535 or port in BLOCKED_CAMERA_PORTS:
        raise NetworkPolicyError("camera port is not allowed")
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkPolicyError("camera hostname did not resolve") from exc
    addresses = {item[4][0].split("%", 1)[0] for item in answers}
    if len(addresses) != 1:
        raise NetworkPolicyError("camera hostname must resolve to one stable private address")
    address = next(iter(addresses))
    if not is_allowed_private_address(address):
        raise NetworkPolicyError("camera must resolve to a private, non-loopback address")
    parsed = ipaddress.ip_address(address)
    if allowed_networks and not any(parsed in network for network in allowed_networks):
        raise NetworkPolicyError("camera is not on the selected directly connected private subnet")
    return address
