from json import dumps, loads
from os import replace
from pathlib import Path
from shutil import copy2, copytree
from sys import argv, stderr
from tempfile import NamedTemporaryFile

CONFIG = Path("/etc/enigma2/telerising.json")
PROFILES = Path("/etc/enigma2/telerising")
RUNTIME = Path("/media/hdd/telerising/runtime")


def profileHome(source):
	if source not in ("standard", "unofficial"):
		raise ValueError("Unknown Telerising source")
	return PROFILES / source


def prepareProfile(home, source):
	# The binary uses its working directory for data. Keep actual profile files
	# inside the normal E2 backup; tar does not follow links to the HDD.
	data = profileHome(source)
	PROFILES.mkdir(mode=0o700, parents=True, exist_ok=True)
	PROFILES.chmod(0o700)
	data.mkdir(mode=0o700, exist_ok=True)
	data.chmod(0o700)
	if not (data / "settings.json").exists() and (home / "settings.json").is_file():
		state = loads((home / "settings.json").read_text(encoding="utf-8"))
		if not isinstance(state.get("accounts"), dict) or not isinstance(state.get("basic"), dict):
			raise ValueError("Invalid Telerising profiles")
		for path in [home / "settings.json", *home.glob("combine-*.json")]:
			copy2(path, data / path.name)
			(data / path.name).chmod(0o600)
		if (home / "cookie_files").is_dir():
			copytree(home / "cookie_files", data / "cookie_files", dirs_exist_ok=True)
	(data / "cookie_files").mkdir(mode=0o700, exist_ok=True)
	(data / "cookie_files").chmod(0o700)
	for path in (data / "cookie_files").rglob("*"):
		path.chmod(0o700 if path.is_dir() else 0o600)
	runtime = RUNTIME / source
	runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
	# Web assets and statistics are not profile data. SQLite resolves the DB
	# link before creating its WAL/SHM files, keeping these on the HDD too.
	for name, target in {"app": home / "app", "version.md": home / "version.md", "stats.db": runtime / "stats.db", "exceptions.txt": runtime / "exceptions.txt"}.items():
		if name in ("app", "version.md") and not target.exists():
			continue
		link = data / name
		if link.is_symlink():
			link.unlink()
		elif link.exists():
			raise ValueError("Unexpected file in Telerising profile directory")
		link.symlink_to(target, target_is_directory=name == "app")
	return data


def transferProfiles(source, target, home, overwrite=True):
	if source == target:
		return
	origin = profileHome(source) / "settings.json"
	destination = profileHome(target) / "settings.json"
	if not origin.is_file():
		return
	previous = loads(origin.read_text(encoding="utf-8"))
	state = loads(destination.read_text(encoding="utf-8")) if destination.is_file() else {"accounts": {}, "basic": previous["basic"]}
	# Read the destination binary's own provider definitions. Unsupported
	# accounts remain in their original variant and its E2 backup.
	catalog = {}
	for path in (home / "app/static/json").glob("providers*.json"):
		definitions = loads(path.read_text(encoding="utf-8"))
		if isinstance(definitions, dict):
			catalog.update({key: value for key, value in definitions.items() if isinstance(value, dict) and "manifest_types" in value})
	changed = False
	for provider, account in previous["accounts"].items():
		metadata = catalog.get(provider)
		if not metadata or not overwrite and provider in state["accounts"]:
			continue
		manifests = [key for key, enabled in metadata["manifest_types"].items() if enabled]
		qualities = metadata.get("available_qualities", ["3000"])
		if not manifests or not qualities:
			continue
		merged = {**state["accounts"].get(provider, {}), **account}
		if merged.get("manifest_type") not in manifests:
			merged["manifest_type"] = manifests[0]
		if merged.get("bw") not in qualities:
			merged["bw"] = "3000" if "3000" in qualities else qualities[0]
		state["accounts"][provider] = merged
		changed = True
	if changed:
		# Keep the destination's web password, UUID and variant-specific settings.
		with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as temporary:
			temporary.write(dumps(state, indent=2))
		try:
			replace(temporary.name, destination)
		finally:
			Path(temporary.name).unlink(missing_ok=True)
		# Cookie-based providers may need their session files after a switch.
		for path in (profileHome(source) / "cookie_files").rglob("*"):
			if path.is_file():
				targetPath = profileHome(target) / "cookie_files" / path.relative_to(profileHome(source) / "cookie_files")
				if overwrite or not targetPath.exists():
					targetPath.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
					copy2(path, targetPath)
					targetPath.chmod(0o600)


if __name__ == "__main__":
	try:
		config = loads(CONFIG.read_text(encoding="utf-8"))
		home = Path(argv[1]).resolve()
		active = config["active"]
		if home.name != active["directory"]:
			raise ValueError("Installation and profile do not match")
		print(prepareProfile(home, active["source"]))
	except Exception as error:
		print(f"Telerising: Could not prepare saved profiles ({type(error).__name__}).", file=stderr)
		raise SystemExit(1)
