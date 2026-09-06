from ipaddress import ip_address, ip_network
from json import loads
from pathlib import Path
from shutil import which
from subprocess import run
from sys import argv
from time import monotonic, sleep


def ensureEndpointRoutes(wait=False):
	wireguard = which("wg")
	if wait:
		# OpenATV launches runlevel 3 init scripts in parallel. A high S-number
		# does not mean DHCP or an enabled WireGuard startup has completed.
		expected = wireguard and any(Path("/etc").glob("rc[2-5].d/S*wireguard"))
		deadline = monotonic() + 30
		previous = None
		stableSince = monotonic()
		print("Telerising: Waiting for network configuration.", flush=True)
		while monotonic() < deadline:
			routes = []
			for family in ("-4", "-6"):
				routes.extend(loads(run(["ip", "-j", family, "route", "show", "table", "main"], check=True, capture_output=True, text=True, timeout=5).stdout))
			peers = run(["wg", "show", "all", "endpoints"], check=True, capture_output=True, text=True, timeout=5).stdout if wireguard else ""
			snapshot = (sorted((route.get("dst", "default"), route.get("gateway", ""), route.get("dev", ""), str(route.get("metric", ""))) for route in routes), peers)
			if snapshot != previous:
				previous = snapshot
				stableSince = monotonic()
			if any(route.get("dst") == "default" for route in routes) and (not expected or any(line.split()[-1] != "(none)" for line in peers.splitlines())) and monotonic() - stableSince >= 3:
				break
			sleep(1)
		else:
			print("Telerising: Network wait expired; checking the current routes.", flush=True)
	if not wireguard:
		return
	interfaces = run(["wg", "show", "interfaces"], check=True, capture_output=True, text=True, timeout=5).stdout.split()
	for interface in interfaces:
		mark = run(["wg", "show", interface, "fwmark"], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
		peers = run(["wg", "show", interface, "endpoints"], check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
		for peer in peers:
			endpoint = peer.split()[1]
			if endpoint == "(none)":
				continue
			address = ip_address(endpoint.rsplit(":", 1)[0].strip("[]"))
			family = f"-{address.version}"
			query = ["ip", "-j", family, "route", "get", str(address)]
			if mark != "off":
				query.extend(["mark", mark])
			current = loads(run(query, check=True, capture_output=True, text=True, timeout=5).stdout)
			# Respect policy routing and deliberately nested VPNs. Only repair a
			# WireGuard interface routing its own endpoint back through itself.
			if not current or current[0].get("dev") != interface:
				continue
			routes = loads(run(["ip", "-j", family, "route", "show", "table", "main"], check=True, capture_output=True, text=True, timeout=5).stdout)
			candidates = []
			for route in routes:
				if not route.get("dev") or route["dev"] in interfaces or route.get("type", "unicast") != "unicast" or any(flag in route.get("flags", []) for flag in ("dead", "linkdown")):
					continue
				network = ip_network(route.get("dst", "default").replace("default", "0.0.0.0/0" if address.version == 4 else "::/0"))
				if address in network:
					candidates.append((network.prefixlen, -int(route.get("metric", 0)), route))
			if not candidates:
				raise RuntimeError("No network route outside WireGuard is available")
			selected = max(candidates, key=lambda item: item[:2])[2]
			args = ["ip", family, "route", "add", f"{address}/{address.max_prefixlen}"]
			if selected.get("gateway"):
				args.extend(["via", selected["gateway"]])
			args.extend(["dev", selected["dev"]])
			# Add only this endpoint's host route; never replace existing routes.
			run(args, check=True, capture_output=True, timeout=5)
			print(f"Telerising: WireGuard endpoint route added via {selected['dev']}.")


if __name__ == "__main__":
	try:
		ensureEndpointRoutes(wait="--wait-network" in argv[1:])
	except Exception as error:
		print(f"Telerising: VPN endpoint route check failed ({type(error).__name__}). Check the network routes.")
		raise SystemExit(1)
