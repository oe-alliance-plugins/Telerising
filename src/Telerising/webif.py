from html import escape
from json import dumps
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from enigma import eDVBDB
from twisted.internet.threads import deferToThread
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET

from . import _, __version__
from .core import Manager, TelerisingError, countryChoices, providerChoices, readConfig
from .releases import ReleaseError


class TelerisingResource(Resource):
	isLeaf = True

	def __init__(self):
		super().__init__()
		self.manager = Manager()

	def reply(self, request, value, status=200):
		request.setResponseCode(status)
		request.setHeader(b"content-type", b"application/json; charset=utf-8")
		return dumps(value, ensure_ascii=False).encode("utf-8")

	def render(self, request):
		request.setHeader(b"cache-control", b"no-store")
		request.setHeader(b"x-content-type-options", b"nosniff")
		if not readConfig()["webif"]:
			return self.reply(request, {"error": _("The Telerising web interface is disabled.")}, 404)
		if request.method not in (b"GET", b"POST"):
			request.setHeader(b"allow", b"GET, POST")
			return self.reply(request, {"error": _("Method not allowed.")}, 405)
		session = request.getSession()
		if not hasattr(session, "telerisingToken"):
			session.telerisingToken = token_urlsafe(32)
		if request.method == b"GET" and request.path in (b"/telerising", b"/telerising/"):
			return self.page(request, session.telerisingToken)
		if request.path != b"/telerising/api":
			return self.reply(request, {"error": _("Unknown request.")}, 404)
		if request.method == b"GET":
			return self.task(request, self.manager.status)
		csrf = request.getHeader("x-telerising-token") or ""
		if not compare_digest(csrf.encode(), session.telerisingToken.encode()):
			return self.reply(request, {"error": _("The session has expired. Reload this page.")}, 403)
		try:
			action = request.args.get(b"action", [b""])[0].decode("ascii")
			provider = request.args.get(b"provider", [b""])[0].decode("ascii")
		except (UnicodeError, IndexError):
			return self.reply(request, {"error": _("Invalid request.")}, 400)
		match action:
			case "web_login":
				def authenticated(client):
					# HTTP cookies apply to the host, including the original UI on
					# port 5000. Keep the managed password out of browser responses.
					cookies = {cookie.name: cookie for cookie in client.cookies if cookie.name in ("session", "sessionID")}
					if not cookies:
						raise TelerisingError(_("Could not open the original Telerising web interface. Please check the server version."))
					for cookie in cookies.values():
						request.addCookie(cookie.name.encode(), cookie.value.encode(), path=b"/", httpOnly=True, sameSite=b"Lax")
					return {"port": 5000}
				return self.task(request, self.manager.client, authenticated)
			case "start" | "stop" | "restart":
				return self.task(request, lambda: self.manager.control(action))
			case "check":
				def checked(release):
					session.telerisingRelease = release
					return {key: release[key] for key in ("source", "tag", "update_available")}
				return self.task(request, self.manager.checkUpdate, checked)
			case "install" if getattr(session, "telerisingRelease", None):
				release = session.telerisingRelease
				session.telerisingRelease = None
				return self.task(request, lambda: self.manager.install(release), lambda value: {"tag": value["tag"]})
			case "providers":
				return self.task(request, self.manager.configured, lambda catalog: {"providers": [{"id": key, "name": value.get("name", key)} for key, value in catalog.items()]})
			case "catalog":
				return self.task(request, self.manager.providers, lambda catalog: {"providers": [{"id": key, "name": value.get("name", key), "configured": value.get("configured", False)} for key, value in sorted(catalog.items(), key=lambda item: (not item[1].get("configured", False), item[1].get("name", item[0]).casefold()))]})
			case "account":
				def account():
					metadata = self.manager.providers().get(provider)
					if not metadata:
						raise TelerisingError(_("Unknown provider."))
					guestAvailable = str(metadata.get("login_required")) == "0"
					countryMode = metadata.get("login_type") == "country"
					if metadata.get("login_type") not in ("username", "email", "country") and not guestAvailable:
						raise TelerisingError(_("This provider requires a special login flow. This plugin version supports username/email login and guest access."))
					stored = self.manager.providerSettings(provider)
					values = stored["account"]
					countries = countryChoices() if countryMode else []
					login = values.get("login", "")
					if countryMode:
						login = login.lower() if login.lower() in dict(countries) else "de"
					manifests, qualities = providerChoices(metadata)
					return {"active": stored["active"], "form": {
						"provider": provider, "name": metadata.get("name", provider), "country_mode": countryMode,
						"countries": countries,
						"login": login,
						"password_saved": not countryMode and bool(values.get("pw")), "guest": values.get("no_auth", False), "guest_available": guestAvailable,
						"manifests": manifests, "qualities": qualities,
						"manifest": next((value for value, label in manifests if value == values.get("manifest_type", "dash")), manifests[0][0]),
						"bandwidth": next((value for value, label in qualities if value == values.get("bw", "3000")), qualities[0][0])
					}}

				def loaded(result):
					token = token_urlsafe(24)
					session.telerisingAccount = {"provider": provider, "active": result["active"], "token": token}
					return {**result["form"], "token": token}
				return self.task(request, account, loaded)
			case "save_account":
				try:
					values = {key: request.args.get(key.encode(), [b""])[0].decode("utf-8") for key in ("login", "password", "guest", "manifest", "bandwidth", "account_token")}
					if any(len(value) > 16384 for value in values.values()) or values["guest"] not in ("true", "false"):
						raise ValueError
				except (UnicodeError, IndexError, ValueError):
					return self.reply(request, {"error": _("Invalid request.")}, 400)
				stored = getattr(session, "telerisingAccount", None)
				if not stored or stored["provider"] != provider or not compare_digest(stored["token"].encode(), values["account_token"].encode()):
					return self.reply(request, {"error": _("Reload the provider settings before saving.")}, 400)

				def saved(result):
					if getattr(session, "telerisingAccount", None) is stored:
						session.telerisingAccount = None
					return {"provider": provider}
				return self.task(request, lambda: self.manager.setupProvider(provider, values["login"], values["password"] or None, values["guest"] == "true", values["manifest"], values["bandwidth"], expected=stored["active"]), saved)
			case "preview":
				def previewed(preview):
					session.telerisingPreview = preview
					return {"count": preview["count"], "names": preview["names"][:6]}
				return self.task(request, lambda: self.manager.preview(provider), previewed)
			case "import" if getattr(session, "telerisingPreview", None):
				preview = session.telerisingPreview
				session.telerisingPreview = None
				def imported(result):
					eDVBDB.getInstance().reloadBouquets()
					return {"channels": result["channels"]}
				return self.task(request, lambda: self.manager.importBouquet(preview), imported)
		return self.reply(request, {"error": _("Unknown action or missing preview. Please try again.")}, 400)

	def task(self, request, function, transform=None):
		connected = [True]
		request.notifyFinish().addErrback(lambda failure: connected.__setitem__(0, False))

		def finish(value):
			result = transform(value) if transform else value
			if connected[0]:
				request.write(self.reply(request, {"success": True, "result": result}))
				request.finish()

		def failed(failure):
			if connected[0]:
				error = failure.value
				message = str(error) if isinstance(error, (TelerisingError, ReleaseError)) else _("Action failed ({error}).").format(error=type(error).__name__)
				request.write(self.reply(request, {"success": False, "error": message}, 400))
				request.finish()

		deferToThread(function).addCallback(finish).addErrback(failed)
		return NOT_DONE_YET

	def page(self, request, csrf):
		labels = {
			"title": _("Local TV server and channel import"),
			"intro": _("Set up providers here or on the receiver. Imported channels appear in the normal TV channel list."),
			"accounts": _("Set up provider"), "loadAccounts": _("Load providers"), "provider": _("Provider"),
			"configured": _("Configured"), "login": _("Username / email"), "password": _("Password"),
			"country": _("Country"), "countryHint": _("Select the country for the Pluto channel list. No account or password is required."),
			"passwordKeep": _("Leave empty to keep the saved password."), "passwordNew": _("Enter the provider password."),
			"showPassword": _("Show password"), "guest": _("Use without signing in"),
			"manifest": _("Stream format"), "bandwidth": _("Picture quality"),
			"saveAccount": _("Save provider settings"), "reloadAccount": _("Reload saved settings"),
			"accountHint": _("Changing the username or guest access briefly restarts the local server."),
			"start": _("Start server"), "stop": _("Stop server"), "restart": _("Restart server"),
			"originalWebif": _("Open original Telerising web interface"),
			"originalHint": _("Advanced settings in the original web interface. Sign-in is handled automatically."),
			"check": _("Install server / check for updates"), "providers": _("Load configured providers"),
			"preview": _("Preview channel import"), "working": _("Working…"),
			"running": _("Running"), "stopped": _("Stopped"), "missing": _("Server not installed"),
			"unreachable": _("Started, but not reachable"),
			"current": _("The latest version is already installed."),
			"confirmInstall": _("Install the selected release? Playing Telerising channels will be interrupted briefly."),
			"confirmImport": _("Import these channels? The previous bouquet for this provider will be backed up and replaced."),
			"done": _("Done."), "empty": _("No provider has been configured yet."),
			"failed": _("Request failed. Please reload the page and check the server status."),
		}
		nonce = token_urlsafe(24)
		request.setHeader(b"content-type", b"text/html; charset=utf-8")
		request.setHeader(b"content-security-policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'".encode())
		page = (Path(__file__).parent / "web/index.html").read_text(encoding="utf-8")
		for key, value in {"NONCE": escape(nonce), "TOKEN": csrf, "VERSION": escape(__version__), "LABELS": dumps(labels, ensure_ascii=False).replace("<", "\\u003c")}.items():
			page = page.replace(f"@@{key}@@", value)
		return page.encode("utf-8")
