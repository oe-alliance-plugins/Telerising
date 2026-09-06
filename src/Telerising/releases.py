"""Resolve and verify public GitHub release assets without a Git dependency."""
from hashlib import sha256
from html.parser import HTMLParser
from re import fullmatch, search
from urllib.parse import unquote
from urllib.request import Request, urlopen

from . import _

REPOSITORIES = {"standard": "DEvmIb/telerising-builds", "unofficial": "DEvmIb/telerising-unofficial"}
MAX_DOWNLOAD = 200 * 1024 * 1024


class ReleaseError(Exception):
	pass


def read_url(url, limit=4 * 1024 * 1024):
	try:
		with urlopen(Request(url, headers={"User-Agent": "Telerising-Enigma2/0.1"}), timeout=25) as response:
			body = response.read(limit + 1)
			if len(body) > limit:
				raise ReleaseError(_("The download server response is too large."))
			return response.url, body
	except (OSError, ValueError) as error:
		raise ReleaseError(_("Download server is unreachable ({error}).").format(error=type(error).__name__)) from None


class AssetsParser(HTMLParser):
	def __init__(self, repo, tag):
		super().__init__()
		self.prefix = f"/{repo}/releases/download/{tag}/"
		self.assets = {}

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag == "a" and attrs.get("href", "").startswith(self.prefix):
			path = attrs["href"]
			name = unquote(path.rsplit("/", 1)[-1])
			self.assets.setdefault(name, {}).update(name=name, url="https://github.com" + path)
		if tag == "clipboard-copy":
			label, digest = attrs.get("aria-label", ""), attrs.get("value", "")
			prefix = "Copy to clipboard digest for "
			if label.startswith(prefix) and fullmatch(r"sha256:[0-9a-f]{64}", digest):
				self.assets.setdefault(label[len(prefix):], {})["sha256"] = digest[7:]


def choose_asset(source, arch, assets):
	suffixes = {
		("standard", "armhf"): r"^telerising_bullseye_armv7l_[0-9a-f]+\.zip$",
		("standard", "aarch64"): r"^telerising_bullseye_aarch64_[0-9a-f]+\.zip$",
		("unofficial", "armhf"): r"^telerising-exp_bullseye_armv7l_[0-9a-f]+\.zip$",
		("unofficial", "aarch64"): r"^telerising-exp_bullseye_aarch64_[0-9a-f]+\.zip$",
	}
	pattern = suffixes.get((source, arch))
	found = [item for item in assets if pattern and search(pattern, item.get("name", ""))]
	if len(found) != 1:
		raise ReleaseError(_("Could not find exactly one matching release download for this receiver."))
	asset = found[0]
	if not fullmatch(r"[0-9a-f]{64}", asset.get("sha256", "")):
		raise ReleaseError(_("No SHA256 digest is available for this download. Installation aborted."))
	return asset


def latest(source, arch):
	repo = REPOSITORIES[source]
	# Public HTML fallback also works when GitHub's unauthenticated API is limited.
	final_url, body = read_url(f"https://github.com/{repo}/releases/latest")
	prefix = f"https://github.com/{repo}/releases/tag/"
	if not final_url.startswith(prefix):
		raise ReleaseError(_("Could not determine the latest release version."))
	tag = final_url[len(prefix):]
	if not fullmatch(r"[A-Za-z0-9_.+-]+", tag):
		raise ReleaseError(_("Unexpected release format."))
	assets_url, body = read_url(f"https://github.com/{repo}/releases/expanded_assets/{tag}")
	parser = AssetsParser(repo, tag)
	parser.feed(body.decode("utf-8"))
	asset = choose_asset(source, arch, list(parser.assets.values()))
	return {**asset, "source": source, "tag": tag, "release_url": final_url}


def download(release, destination):
	prefix = f"https://github.com/{REPOSITORIES[release['source']]}/releases/download/{release['tag']}/"
	if not release["url"].startswith(prefix):
		raise ReleaseError(_("Download source is not allowed."))
	digest = sha256()
	total = 0
	try:
		with urlopen(Request(release["url"], headers={"User-Agent": "Telerising-Enigma2/0.1"}), timeout=30) as response, destination.open("xb") as output:
			while chunk := response.read(256 * 1024):
				total += len(chunk)
				if total > MAX_DOWNLOAD:
					raise ReleaseError(_("The download exceeds the size limit."))
				digest.update(chunk)
				output.write(chunk)
	except OSError:
		raise ReleaseError(_("The release download was interrupted.")) from None
	if digest.hexdigest() != release["sha256"]:
		raise ReleaseError(_("SHA256 mismatch. The download will not be installed."))
	return total
