from contextlib import contextmanager
from copy import deepcopy
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from functools import wraps
from http.cookiejar import CookieJar
from json import dumps, loads
from os import chmod, replace, symlink
from pathlib import Path
from platform import libc_ver, machine
from re import fullmatch, search
from secrets import token_urlsafe
from shutil import copytree, disk_usage, rmtree
from socket import gethostbyname_ex, gethostname
from struct import calcsize, unpack_from
from subprocess import run
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import uuid4
from zipfile import ZipFile
from Components.config import configfile
from Components.Network import iNetwork

from . import _
from .playlist import atomic_write, parse, publish, render
from .profiles import prepareProfile, profileHome, transferProfiles
from .releases import download, latest
from .settings import settings

CONFIG = Path("/etc/enigma2/telerising.json")
ROOT = Path("/media/hdd/telerising")
INIT = "/etc/init.d/telerising"


class TelerisingError(Exception):

	pass


def countryChoices():
	# Names from oe-alliance/PlutoTV's plutotv.xml; codes confirmed by the
	# documented country validation of Unofficial 7.5.3 (a subset of that XML).
	countries = [("ar", _("Argentina")), ("br", _("Brazil")), ("ca", _("Canada")), ("cl", _("Chile")), ("dk", _("Denmark")), ("fr", _("France")), ("de", _("Germany")), ("it", _("Italy")), ("mx", _("Mexico")), ("no", _("Norway")), ("es", _("Spain")), ("se", _("Sweden")), ("gb", _("United Kingdom")), ("us", _("United States"))]
	return sorted(countries, key=lambda country: country[1].casefold())


def providerChoices(metadata):
	manifests = [(key, "DASH" if key == "dash" else "HLS") for key, enabled in metadata.get("manifest_types", {}).items() if enabled]
	qualities = [(value, {"1500": "432p25", "2999": "576p50", "3000": "720p25", "5000": "720p50", "4999": "1080p25", "8000": "1080p50"}.get(value, value)) for value in metadata.get("available_qualities", ["3000"])]
	if not manifests or not qualities:
		raise TelerisingError(_("The provider or manifest type is not supported."))
	return manifests, qualities


@contextmanager
def operation():
	with open("/run/telerising-manager.lock", "a") as lock:
		try:
			flock(lock, LOCK_EX | LOCK_NB)
		except BlockingIOError:
			raise TelerisingError(_("A Telerising operation is already running.")) from None
		try:
			yield
		finally:
			flock(lock, LOCK_UN)


def exclusive(function):
	@wraps(function)
	def wrapped(*args, **kwargs):
		with operation():
			return function(*args, **kwargs)
	return wrapped


def readConfig():
	defaults = {"admin": {}, "active": None, "previous": None, "installed": {}, "imports": {}, "jobs": {}, "notified_updates": {}}
	if CONFIG.exists():
		defaults.update(loads(CONFIG.read_text(encoding="utf-8")))
	defaults.update(source=settings.source.value, webif=settings.webif.value, service_type=int(settings.service_type.value), update_toast=settings.update_toast.value)
	defaults["schedule"] = {
		"update_interval": settings.update_interval.value,
		"update_time": list(settings.update_time.value),
		"update_day": int(settings.update_day.value),
		"channels_interval": settings.channels_interval.value,
		"channels_time": list(settings.channels_time.value),
		"channels_day": int(settings.channels_day.value),
		"standby_only": settings.standby_only.value,
	}
	return defaults


def saveConfig(config):
	CONFIG.parent.mkdir(parents=True, exist_ok=True)
	state = {key: value for key, value in config.items() if key not in ("source", "webif", "service_type", "schedule", "update_toast")}
	atomic_write(CONFIG, dumps(state, indent=2))


def platformInfo():
	model = ""
	image = Path("/etc/image-version")
	if image.exists():
		values = dict(line.split("=", 1) for line in image.read_text().splitlines() if "=" in line)
		model = values.get("box_type", values.get("machinemake", ""))
	arch = "aarch64" if machine() == "aarch64" and calcsize("P") == 8 else "armhf" if machine().startswith("arm") and calcsize("P") == 4 else "unsupported"
	return {"model": model, "arch": arch, "glibc": libc_ver()[1], "supported": (model == "sf8008" and arch == "armhf") or (model == "dreamtwo" and arch == "aarch64"), "experimental": model == "dreamtwo"}


def checkedHome(name):
	if not name or not fullmatch(r"[a-zA-Z0-9_.-]+", name):
		raise TelerisingError(_("Invalid installation state."))
	path = ROOT / "releases" / name
	if path.is_symlink() or path.resolve().parent != (ROOT / "releases").resolve():
		raise TelerisingError(_("Invalid installation path."))
	return path


def activate(name):
	home = checkedHome(name)
	if not (home / "api").is_file():
		raise TelerisingError(_("The selected installation is missing."))
	temporary = ROOT / f"current-{uuid4().hex}"
	symlink(str(home), temporary)
	replace(temporary, ROOT / "current")


def service(action):
	if action not in ("start", "stop", "restart", "status"):
		raise TelerisingError(_("Unknown service action."))
	result = run([INIT, action], capture_output=True, text=True, timeout=90)
	if action == "status":
		return result.returncode == 0
	if result.returncode == 6:
		raise TelerisingError(_("Telerising storage is not mounted. Please check /media/hdd and start the server again."))
	if result.returncode == 7:
		raise TelerisingError(_("The VPN endpoint route could not be prepared. Please check the receiver's network routes."))
	if result.returncode == 8:
		raise TelerisingError(_("Could not prepare the saved provider profiles. Please check the Telerising startup log."))
	if result.returncode:
		raise TelerisingError(_("Service action failed. Please check the Telerising log."))
	return True


def httpReady():
	try:
		with build_opener(ProxyHandler({})).open("http://127.0.0.1:5000/", timeout=2) as response:
			return response.status == 200
	except (OSError, URLError):
		return False


def ready(timeout=40):
	deadline = monotonic() + timeout
	while monotonic() < deadline:
		if httpReady():
			return
		sleep(0.5)
	raise TelerisingError(_("Telerising did not become ready. Please check the log."))


class ApiClient:

	def __init__(self, config):
		self.config = config
		self.cookies = CookieJar()
		self.client = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))
		self.base = "http://127.0.0.1:5000"

	def request(self, path, data=None, limit=8 * 1024 * 1024):
		if not path.startswith("/") or path.startswith("//"):
			raise TelerisingError(_("Invalid API request."))
		request = Request(self.base + path, data=None if data is None else urlencode(data).encode())
		try:
			with self.client.open(request, timeout=25) as response:
				body = response.read(limit + 1)
				if len(body) > limit:
					raise TelerisingError(_("The Telerising response is too large."))
				return response.url, body
		except (OSError, URLError, ValueError):
			raise TelerisingError(_("Telerising is unreachable or rejected the request.")) from None

	def api(self, path, data=None):
		url, body = self.request(path, data)
		try:
			result = loads(body)
		except ValueError:
			raise TelerisingError(_("Invalid API response. Check the login and server version.")) from None
		if not isinstance(result, dict) or result.get("success") is False:
			if isinstance(result, dict) and result.get("message") == "wvd missing":
				raise TelerisingError(_("Telerising requires a Widevine device file (WVD) for this provider, but it is missing. Account login could not be checked."))
			raise TelerisingError(_("Telerising rejected the request. Check the provider, login details and account permissions."))
		return result

	def login(self):
		active = self.config.get("active")
		if not active:
			raise TelerisingError(_("Please install Telerising first."))
		password = self.config["admin"].get(active["source"])
		if not password:
			raise TelerisingError(_("The Telerising web password is missing."))
		url, body = self.request("/")
		self.api("/api/signup_check" if url.endswith("/signup") else "/api/login_check", {"pw": password})

	def providers(self):
		url, body = self.request("/setup")
		version = search(rb'var version\s*=\s*"([A-Za-z0-9_.-]+)"', body)
		if not version:
			raise TelerisingError(_("The provider definition in this version is not supported yet."))
		url, body = self.request(f"/static/json/providers-{version[1].decode()}.json")
		result = loads(body)
		if not isinstance(result, dict):
			raise TelerisingError(_("Invalid provider definition."))
		catalog = {key: value for key, value in result.items() if fullmatch(r"[A-Za-z0-9_-]+", key) and isinstance(value, dict)}
		# Unofficial's docs/pluto.md specifies a country code and empty password,
		# despite the bundled provider definition calling the field "email".
		if self.config["active"]["source"] == "unofficial" and catalog.get("plu", {}).get("module") == "pluto":
			catalog["plu"]["login_type"] = "country"
		return catalog


class Manager:

	def status(self):
		config = readConfig()
		running = Path(INIT).exists() and service("status")
		return {"platform": platformInfo(), "active": config["active"], "source": config["source"], "webif": config["webif"], "service_type": config["service_type"], "running": running, "ready": running and httpReady(), "autostart": bool(list(Path("/etc/rc2.d").glob("S*telerising"))), "jobs": config["jobs"]}

	@exclusive
	def recordJob(self, kind, result):
		config = readConfig()
		config["jobs"][kind] = {"checked_at": int(time()), **result}
		saveConfig(config)

	def checkUpdate(self):
		config, info = readConfig(), platformInfo()
		if not info["supported"]:
			raise TelerisingError(_("The local Telerising server is not supported on this model."))
		release = latest(config["source"], info["arch"])
		active = config.get("active") or {}
		release["update_available"] = any(active.get(key) != release[key] for key in ("source", "tag", "sha256")) or not (checkedHome(active["directory"]) / "api").is_file()
		return release

	@exclusive
	def install(self, release):
		info, config = platformInfo(), readConfig()
		originalConfig = deepcopy(config)
		if not info["supported"] or release["source"] != config["source"]:
			raise TelerisingError(_("The device or selected download source has changed."))
		if not Path("/media/hdd").is_mount():
			raise TelerisingError(_("Please mount a storage device at /media/hdd first."))
		if disk_usage("/media/hdd").free < 650 * 1024 * 1024:
			raise TelerisingError(_("At least 650 MB of free storage is required."))
		ROOT.mkdir(mode=0o700, exist_ok=True)
		(ROOT / "releases").mkdir(mode=0o700, exist_ok=True)
		stage = ROOT / f"staging-{uuid4().hex}"
		stage.mkdir(mode=0o700)
		old = config.get("active")
		wasRunning = service("status")
		stateChanged = False
		profileChanged = False
		data = profileHome(release["source"])
		try:
			download(release, stage / "download.zip")
			unpacked = stage / "unpacked"
			unpacked.mkdir()
			with ZipFile(stage / "download.zip") as archive:
				if sum(item.file_size for item in archive.infolist()) > 400 * 1024 * 1024:
					raise TelerisingError(_("The extracted installation is too large."))
				for item in archive.infolist():
					path = (unpacked / item.filename).resolve()
					if not path.is_relative_to(unpacked.resolve()) or ((item.external_attr >> 16) & 0o170000) == 0o120000:
						raise TelerisingError(_("Unsafe path in the release archive."))
				archive.extractall(unpacked)
			home = unpacked / "telerising" if (unpacked / "telerising/api").exists() else unpacked
			binary = home / "api"
			with binary.open("rb") as stream:
				header = stream.read(64)
			expected = (1, 40) if info["arch"] == "armhf" else (2, 183)
			if len(header) < 64 or header[:4] != b"\x7fELF" or header[5] != 1 or (header[4], unpack_from("<H", header, 18)[0]) != expected:
				raise TelerisingError(_("The binary does not match the receiver's userspace."))
			chmod(binary, 0o755)
			service("stop")
			for prior in [*config["installed"].values(), *([old] if old else [])]:
				previous = checkedHome(prior["directory"])
				if previous.is_dir():
					prepareProfile(previous, prior["source"])
			if data.exists():
				copytree(data, stage / "profiles", symlinks=True)
			if not (data / "settings.json").is_file():
				config["admin"][release["source"]] = config["admin"].get(old["source"], token_urlsafe(24)) if old else token_urlsafe(24)
			name = f"{release['source']}-{uuid4().hex}"
			replace(home, checkedHome(name))
			profileChanged = True
			prepareProfile(checkedHome(name), release["source"])
			if old:
				transferProfiles(old["source"], release["source"], checkedHome(name))
			active = {key: release[key] for key in ("source", "tag", "sha256", "name", "release_url")}
			active["directory"] = name
			config.update(active=active, previous=old, profiles_version=1)
			if old:
				config["installed"][old["source"]] = old
			config["installed"][release["source"]] = active
			stateChanged = True
			saveConfig(config)
			activate(name)
			service("start")
			ready()
			client = ApiClient(config)
			client.login()
			return active
		except Exception:
			if profileChanged:
				service("stop")
				rmtree(data)
				if (stage / "profiles").is_dir():
					copytree(stage / "profiles", data, symlinks=True)
			if stateChanged:
				service("stop")
				if old and (checkedHome(old["directory"]) / "api").is_file():
					activate(old["directory"])
				else:
					(ROOT / "current").unlink(missing_ok=True)
				saveConfig(originalConfig)
			if old and wasRunning and (checkedHome(old["directory"]) / "api").is_file():
				service("start")
			raise
		finally:
			# stage is created by this operation and constrained to ROOT.
			if stage.resolve().parent == ROOT.resolve():
				rmtree(stage)

	@exclusive
	def rollback(self):
		config = readConfig()
		originalConfig = deepcopy(config)
		previous, active = config.get("previous"), config.get("active")
		if not previous or not (checkedHome(previous["directory"]) / "api").is_file():
			raise TelerisingError(_("No previous installation is available."))
		wasRunning = service("status")
		service("stop")
		data = profileHome(previous["source"])
		with TemporaryDirectory(prefix="rollback-", dir=ROOT) as directory:
			backup = Path(directory) / "profiles"
			if data.exists():
				copytree(data, backup, symlinks=True)
			try:
				prepareProfile(checkedHome(previous["directory"]), previous["source"])
				transferProfiles(active["source"], previous["source"], checkedHome(previous["directory"]))
				activate(previous["directory"])
				config.update(active=previous, previous=active)
				saveConfig(config)
				service("start")
				ready()
				ApiClient(config).login()
			except Exception:
				service("stop")
				if data.exists():
					rmtree(data)
				if backup.exists():
					copytree(backup, data, symlinks=True)
				prepareProfile(checkedHome(active["directory"]), active["source"])
				activate(active["directory"])
				saveConfig(originalConfig)
				if wasRunning:
					service("start")
				raise

	@exclusive
	def control(self, action):
		service(action)
		if action in ("start", "restart"):
			ready()

	@exclusive
	def autostart(self, enabled):
		args = ["update-rc.d", "-f", "telerising"] + (["defaults", "95"] if enabled else ["remove"])
		if run(args, capture_output=True, timeout=20).returncode:
			raise TelerisingError(_("Could not change autostart."))
		settings.autostart.value = enabled
		settings.autostart.save()
		configfile.save()

	@exclusive
	def restoreSettings(self, reload=False):
		config = readConfig()
		active = config.get("active")
		if not active:
			return
		enabled = bool(list(Path("/etc/rc2.d").glob("S*telerising")))
		if not config.get("profiles_version"):
			settings.autostart.value = enabled
			settings.autostart.save()
			configfile.save()
		if enabled != settings.autostart.value:
			args = ["update-rc.d", "-f", "telerising"] + (["defaults", "95"] if settings.autostart.value else ["remove"])
			if run(args, capture_output=True, timeout=20).returncode:
				raise TelerisingError(_("Could not change autostart."))
		home = checkedHome(active["directory"])
		if not Path("/media/hdd").is_mount() or not (home / "api").is_file():
			# A restored backup contains profiles, not the downloaded binary.
			return
		running = service("status")
		restart = running and (reload or Path(f"/proc/{Path('/run/telerising.pid').read_text().strip()}/cwd").resolve() != profileHome(active["source"]))
		if running and not restart and config.get("profiles_version"):
			return
		if restart:
			service("stop")
		try:
			for prior in [*config["installed"].values(), active]:
				previous = checkedHome(prior["directory"])
				if previous.is_dir():
					prepareProfile(previous, prior["source"])
			if not config.get("profiles_version"):
				# Upgrade existing plugin installations, including a previous switch
				# that left the active variant with no accounts. Existing ones win.
				for source in ("standard", "unofficial"):
					transferProfiles(source, active["source"], home, overwrite=False)
				config["profiles_version"] = 1
				saveConfig(config)
			activate(active["directory"])
		finally:
			if restart or settings.autostart.value and not running:
				service("start")
				ready()

	def client(self):
		client = ApiClient(readConfig())
		client.login()
		return client

	def providers(self, configured=False):
		client = self.client()
		catalog = client.providers()
		used = client.api("/api/provider_check").get("message", [])
		return {key: {**value, "configured": key in used} for key, value in catalog.items() if not configured or key in used}

	def configured(self):
		return self.providers(configured=True)

	def providerSettings(self, provider):
		active = readConfig().get("active")
		if not active:
			raise TelerisingError(_("Please install Telerising first."))
		try:
			accounts = loads((profileHome(active["source"]) / "settings.json").read_text(encoding="utf-8"))["accounts"]
			account = accounts.get(provider, {})
			if not isinstance(account, dict) or account and (any(not isinstance(account.get(key), str) for key in ("login", "pw", "manifest_type", "bw")) or not isinstance(account.get("no_auth"), bool)):
				raise ValueError
		except (OSError, ValueError, KeyError, TypeError, AttributeError):
			raise TelerisingError(_("Could not read the saved provider settings.")) from None
		return {"active": active, "account": account}

	@exclusive
	def setupProvider(self, provider, login, password, guest, manifest, bandwidth, expected=None):
		client = self.client()
		stored = self.providerSettings(provider)
		if expected is not None and stored["active"] != expected:
			raise TelerisingError(_("The server version changed. Reopen the provider settings."))
		account = stored["account"]
		metadata = client.providers().get(provider)
		if not metadata or not metadata.get("manifest_types", {}).get(manifest):
			raise TelerisingError(_("The provider or manifest type is not supported."))
		if guest and str(metadata.get("login_required")) != "0":
			raise TelerisingError(_("This provider requires a login."))
		if not guest and metadata.get("login_type") not in ("username", "email", "country"):
			raise TelerisingError(_("This provider requires a special login flow. This plugin version supports username/email login and guest access."))
		if bandwidth not in metadata.get("available_qualities", ["3000"]):
			raise TelerisingError(_("The selected picture quality is not supported."))
		if metadata.get("login_type") == "country":
			login = login.strip().lower()
			if login not in dict(countryChoices()):
				raise TelerisingError(_("Please select a supported country from the list."))
			password = ""
		elif password is None:
			password = account.get("pw", "")
		if not guest and metadata.get("login_type") != "country" and (not login.strip() or not password):
			raise TelerisingError(_("Please enter a username/email and password."))
		form = {"id": provider, "login": login, "pw": password, "no_auth": "true" if guest else "false"}
		stream = {"manifest_type": manifest, "bw": bandwidth, "audio1": account.get("audio1", "aac1"), "audio2": account.get("audio2", "none")}
		if not account:
			client.api("/api/login", form)
			client.api("/api/new", {**form, **stream, "rt": ""})
		elif login != account["login"] or guest != account["no_auth"]:
			client.api("/api/login", form)
			# The server API only edits passwords. Apply username/guest changes while stopped.
			path = profileHome(stored["active"]["source"]) / "settings.json"
			original = None
			service("stop")
			try:
				original = path.read_text(encoding="utf-8")
				state = loads(original)
				state["accounts"][provider].update(login=login, pw=password, no_auth=guest, refresh_token="", manifest_type=manifest, bw=bandwidth)
				atomic_write(path, dumps(state, indent=2))
				service("start")
				ready()
			except Exception:
				service("stop")
				if original is not None:
					atomic_write(path, original)
				service("start")
				ready()
				client.api("/api/login", {"id": provider, "login": account["login"], "pw": account["pw"], "no_auth": "true" if account["no_auth"] else "false"})
				raise
		else:
			if password != account["pw"]:
				client.api("/api/login", form)
				client.api(f"/api/{provider}/save/account_pw", {"pw": password})
			if manifest != account["manifest_type"] or bandwidth != account["bw"]:
				client.api(f"/api/{provider}/save/manifest", stream)

	def preview(self, provider):
		client = self.client()
		catalog = client.providers()
		if provider not in catalog:
			raise TelerisingError(_("Unknown provider."))
		url, body = client.request(f"/api/{provider}/file/channels.m3u")
		channels = parse(body.decode("utf-8-sig"))
		serviceType = readConfig()["service_type"]
		if serviceType in (5001, 5002) and not Path("/usr/lib/enigma2/python/Plugins/SystemPlugins/ServiceApp/serviceapp.so").exists():
			raise TelerisingError(_("Please install ServiceApp and restart Enigma2 before using this playback profile."))
		try:
			hosts = gethostbyname_ex(gethostname())[2]
		except OSError:
			hosts = []
		# Include configured box interfaces without probing any other network host.
		hosts += [".".join(map(str, iNetwork.getAdapterAttribute(adapter, "ip"))) for adapter in iNetwork.getAdapterList() if iNetwork.getAdapterAttribute(adapter, "ip")]
		content = render(channels, f"Telerising · {catalog[provider].get('name', provider)}", provider, serviceType, 5000, hosts)
		return {"provider": provider, "source": client.config["active"]["source"], "tag": client.config["active"]["tag"], "count": len(channels), "matched": sum(bool(channel["dvb_reference"]) for channel in channels), "names": [channel["name"] for channel in channels[:12]], "content": content}

	@exclusive
	def importBouquet(self, preview):
		config = readConfig()
		active = config.get("active") or {}
		if any(preview.get(key) != active.get(key) for key in ("source", "tag")):
			raise TelerisingError(_("The server version changed. Fetch a new channel preview."))
		result = publish("/etc/enigma2", preview["provider"], preview["content"])
		config["imports"][preview["provider"]] = {"source": active["source"], "file": result["file"]}
		saveConfig(config)
		return result
