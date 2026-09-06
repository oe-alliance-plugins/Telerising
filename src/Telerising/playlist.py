"""M3U parsing and atomic publication of plugin-owned Enigma2 bouquets."""
from hashlib import sha256
from os import O_CREAT, O_TRUNC, O_WRONLY, chmod, fdopen, fsync, open as openFile, replace
from pathlib import Path
from re import findall, fullmatch, match
from shutil import copy2, rmtree
from time import strftime, time_ns
from unicodedata import normalize
from urllib.parse import urlsplit, urlunsplit

from . import _


def clean(value):
	return " ".join(value.replace("\x00", "").split())


def parse(text):
	if not text.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"):
		raise ValueError(_("No M3U channel list was received."))
	pending = None
	seen = set()
	result = []
	for raw in text.lstrip("\ufeff").splitlines():
		line = raw.strip()
		if line.startswith("#EXTINF:"):
			entry = match(r'^#EXTINF:((?:[^",]|"[^"]*")*),(.*)$', line)
			if not entry:
				raise ValueError(_("Invalid channel entry."))
			pending = (clean(entry[2]), dict(findall(r'([\w-]+)="([^"]*)"', entry[1])))
		elif line and not line.startswith("#"):
			parts = urlsplit(line)
			if parts.scheme not in ("http", "https") or not parts.hostname or any(ord(c) < 33 for c in line):
				raise ValueError(_("This list contains unsupported stream URLs."))
			if not pending or not pending[0]:
				raise ValueError(_("Missing channel name."))
			if line not in seen:
				result.append({"name": pending[0], "attributes": pending[1], "url": line})
				seen.add(line)
			pending = None
	if pending or not result:
		raise ValueError(_("The channel list is empty or incomplete."))
	return result


def channelKey(name):
	# Keep region and HD/UHD qualifiers; similar names can carry different schedules.
	return "".join(char for char in normalize("NFKC", name).casefold() if char.isalnum() or char in "+&")


def render(channels, title, provider, service_type, port, local_hosts, service_directory="/etc/enigma2"):
	if service_type not in (4097, 5001, 5002) or not fullmatch(r"[a-zA-Z0-9_-]+", provider):
		raise ValueError(_("Invalid channel profile."))
	references = {}
	# Read E2's saved database in the worker; eServiceCenter belongs to the GUI thread.
	for filename in ("lamedb", "lamedb5"):
		path = Path(service_directory) / filename
		if not path.exists():
			continue
		lines = iter(path.read_text(encoding="utf-8", errors="replace").splitlines())
		header = next(lines, "")
		if header not in ("eDVB services /4/", "eDVB services /5/"):
			continue
		version5 = header.endswith("/5/")
		if not version5:
			for line in lines:
				if line == "services":
					break
		for line in lines:
			if version5:
				entry = match(r'^s:([^,]+),"([^"]*)"', line)
				if not entry:
					continue
				fields, name = entry[1].split(":"), entry[2]
			else:
				if line == "end":
					break
				fields, name = line.split(":"), next(lines, "")
				next(lines, "")  # Provider and cached PIDs.
			if len(fields) < 6:
				continue
			try:
				sid, namespace, tsid, onid = (int(value, 16) for value in fields[:4])
				kind = int(fields[4])  # lamedb stores the DVB type in decimal.
			except ValueError:
				continue
			if kind not in (1, 17, 22, 25, 31) or not sid or not name:
				continue
			ref = f"1:0:{kind:X}:{sid:X}:{tsid:X}:{onid:X}:{namespace:X}:0:0:0"
			references.setdefault(channelKey(name), set()).add(ref)
		break  # lamedb and lamedb5 are alternative exports of the same database.
	rows = ["#NAME " + clean(title)]
	for channel in channels:
		parts = urlsplit(channel["url"])
		if parts.hostname not in set(local_hosts) | {"127.0.0.1", "localhost"} or not parts.path.startswith("/api/") or (parts.port or 80) != port:
			raise ValueError(_("The playlist contains URLs that do not point to the local Telerising server."))
		url = urlunsplit(("http", f"127.0.0.1:{port}", parts.path, parts.query, parts.fragment))
		# Retain stable identities for channels without an unambiguous DVB match.
		key = provider + ":" + (channel["attributes"].get("tvg-id") or parts.path)
		digest = sha256(key.encode()).hexdigest()
		sid, namespace = digest[:4], digest[4:12]
		name = clean(channel["name"])
		matches = references.get(channelKey(name), set())
		if len(matches) > 1:
			# Prefer the common Astra 19.2E reference when the same name is on several satellites.
			astra = {ref for ref in matches if ref.split(":")[6] == "C00000"}
			if len(astra) == 1:
				matches = astra
		channel["dvb_reference"] = next(iter(matches)) if len(matches) == 1 else None
		identity = channel["dvb_reference"].split(":", 1)[1] if channel["dvb_reference"] else f"0:1:{sid}:0:0:{namespace}:0:0:0"
		ref = f"{service_type}:{identity}:{url.replace(':', '%3a')}:{name}"
		rows.extend(("#SERVICE " + ref, "#DESCRIPTION " + name))
	return "\n".join(rows) + "\n"


def atomic_write(path, text, mode=0o600):
	temp = path.with_name(path.name + ".new")
	fd = openFile(temp, O_WRONLY | O_CREAT | O_TRUNC, mode)
	with fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
		stream.write(text)
		stream.flush()
		fsync(stream.fileno())
	chmod(temp, mode)
	replace(temp, path)


def pruneBackups(directory, keep=None):
	root = Path(directory) / "telerising-backups"
	if root.is_symlink():
		raise ValueError(_("Invalid Telerising backup directory."))
	if not root.exists():
		return 0
	backups = sorted((path for path in root.iterdir() if fullmatch(r"\d{8}-\d{6}-\d+", path.name) and path.is_dir() and not path.is_symlink() and path.resolve().parent == root.resolve()), reverse=True)
	if keep is None:
		keep = backups[0] if backups else None
	elif keep not in backups:
		raise ValueError(_("Invalid Telerising backup directory."))
	removed = 0
	for path in backups:
		if path != keep:
			rmtree(path)
			removed += 1
	return removed


def publish(directory, provider, content):
	directory = Path(directory)
	if not fullmatch(r"[a-zA-Z0-9_-]+", provider):
		raise ValueError(_("Invalid provider ID."))
	name = f"userbouquet.telerising_{provider}.tv"
	bouquet = directory / name
	index = directory / "bouquets.tv"
	reference = f'1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "{name}" ORDER BY bouquet'
	marker = '#SERVICE ' + reference
	old = index.read_text(encoding="utf-8") if index.exists() else "#NAME Bouquets (TV)\n"
	result = {"file": name, "channels": content.count("\n#SERVICE "), "reference": reference}
	if bouquet.exists() and bouquet.read_text(encoding="utf-8") == content and marker in old.splitlines():
		pruneBackups(directory)
		return result
	root = directory / "telerising-backups"
	if root.is_symlink():
		raise ValueError(_("Invalid Telerising backup directory."))
	backup = root / f"{strftime('%Y%m%d-%H%M%S')}-{time_ns()}"
	backup.mkdir(parents=True, mode=0o700)
	published = False
	try:
		if bouquet.exists():
			copy2(bouquet, backup / name)
		if index.exists():
			copy2(index, backup / "bouquets.tv")
		atomic_write(bouquet, content)
		published = True
		if marker not in old.splitlines():
			atomic_write(index, old.rstrip() + "\n" + marker + "\n")
	except Exception:
		if published:
			if (backup / name).exists():
				copy2(backup / name, bouquet)
			else:
				bouquet.unlink()
		if not backup.is_symlink() and backup.resolve().parent == root.resolve():
			rmtree(backup)
		raise
	pruneBackups(directory, keep=backup)
	return result
