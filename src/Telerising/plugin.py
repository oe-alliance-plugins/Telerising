from enigma import eDVBDB, getDesktop
from Components.ActionMap import HelpableActionMap
from Components.config import configfile, setOnSaveCallback
from Components.Label import Label
from Components.MenuList import MenuList
from Components.Sources.StaticText import StaticText
from Screens.ChoiceBox import ChoiceBox
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from twisted.internet import reactor
from twisted.internet.threads import deferToThread
from Plugins.Extensions.EPGImport.plugin import main as epgImport
from Plugins.Plugin import PluginDescriptor

from . import PluginLanguageDomain, _, __version__
from .core import Manager, TelerisingError, countryChoices, providerChoices, readConfig
from .releases import REPOSITORIES, ReleaseError
from .scheduler import register, synchronize
from .settings import settings
from .webif import setupWeb


def refreshWebMenu():
	configfile.save()
	deferToThread(Manager().restoreSettings).addErrback(lambda failure: print(f"[Telerising] Could not restore service settings: {failure.type.__name__}"))
	setupWeb()


class ProviderSetup(Setup):

	def __init__(self, session, manager, provider, metadata, stored):
		self.manager = manager
		self.provider = provider
		self.active = stored["active"]
		self.busy = False
		account = stored["account"]
		countryMode = metadata.get("login_type") == "country"
		countries = countryChoices()
		settings.provider.country.setChoices(countries, default=account.get("login", "").lower() if countryMode and account.get("login", "").lower() in dict(countries) else "de")
		manifests, qualities = providerChoices(metadata)
		settings.provider.manifest.setChoices(manifests, default=next((value for value, label in manifests if value == account.get("manifest_type", "dash")), manifests[0][0]))
		settings.provider.bandwidth.setChoices(qualities, default=next((value for value, label in qualities if value == account.get("bw", "3000")), qualities[0][0]))
		for key, value in {
			"country_mode": countryMode,
			"country": settings.provider.country.default,
			"login": "" if countryMode else account.get("login", ""),
			"password": "" if countryMode else account.get("pw", ""),
			"guest": account.get("no_auth", False), "guest_available": str(metadata.get("login_required")) == "0",
			"manifest": settings.provider.manifest.default, "bandwidth": settings.provider.bandwidth.default
		}.items():
			item = getattr(settings.provider, key)
			# NoSave defaults provide native change/cancel handling without a second saved copy.
			item.default = value
			item.saved_value = None
			item.load()
		Setup.__init__(self, session, "telerisingprovider", plugin="Extensions/Telerising", PluginLanguageDomain=PluginLanguageDomain)
		self.setTitle(metadata.get("name", provider))
		self.onClose.append(self.clearCredentials)

	def formatItemDescription(self, item, itemDescription, data=None):
		if item is settings.provider.login or item is settings.provider.password:
			return self.formatItemText(itemDescription, data)
		return Setup.formatItemDescription(self, item, itemDescription, data)

	def keySave(self):
		if self.busy:
			return
		values = {key: getattr(settings.provider, key).value for key in ("login", "password", "guest", "manifest", "bandwidth")}
		if settings.provider.country_mode.value:
			values.update(login=settings.provider.country.value, password="")
		self.busy = True
		self.suspendAllActionMaps()
		self.setFootnote(_("Setting up provider…"))

		def failed(failure):
			self.busy = False
			self.resumeAllActionMaps()
			self.setFootnote("")
			error = failure.value
			message = str(error) if isinstance(error, (TelerisingError, ReleaseError)) else _("Action failed ({error}).").format(error=type(error).__name__)
			self.session.open(MessageBox, message, MessageBox.TYPE_ERROR)

		deferred = deferToThread(self.manager.setupProvider, self.provider, **values, expected=self.active)
		deferred.addCallbacks(lambda result: self.close(), failed)

	def clearCredentials(self):
		for item in (settings.provider.login, settings.provider.password):
			item.default = ""
			item.saved_value = None
			item.load()
			item.lastValue = ""


class TelerisingSetup(Screen):

	skin = """
	<screen name="TelerisingSetup" position="center,center" size="980,580" resolution="1280,720" title="Telerising">
		<widget name="status" position="20,15" size="940,85" font="Regular;24" />
		<widget name="menu" position="20,110" size="940,385" scrollbarMode="showOnDemand" />
		<widget source="key_red" render="Label" position="20,515" size="210,40" backgroundColor="key_red" foregroundColor="key_text" font="Regular;22" halign="center" valign="center" />
		<widget source="key_green" render="Label" position="245,515" size="210,40" backgroundColor="key_green" foregroundColor="key_text" font="Regular;22" halign="center" valign="center" />
		<widget source="key_yellow" render="Label" position="470,515" size="210,40" backgroundColor="key_yellow" foregroundColor="key_text" font="Regular;22" halign="center" valign="center" />
		<widget source="key_blue" render="Label" position="695,515" size="265,40" backgroundColor="key_blue" foregroundColor="key_text" font="Regular;22" halign="center" valign="center" />
		<widget source="key_info" render="Label" position="20,558" size="940,20" font="Regular;18" halign="center" />
	</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.manager = Manager()
		self.busy = False
		self.state = {}
		self.setTitle(f"Telerising · {__version__}")
		self["status"] = Label(_("Reading status…"))
		self["menu"] = MenuList([])
		self["key_red"] = StaticText(_("Close"))
		self["key_green"] = StaticText(_("Select"))
		self["key_yellow"] = StaticText(_("Import channels"))
		self["key_blue"] = StaticText(_("Settings"))
		self["key_info"] = StaticText(_("INFO: About Telerising"))
		self["actions"] = HelpableActionMap(self, ["OkCancelActions", "ColorActions", "InfoActions"], {
			"ok": (self.select, _("Run the selected action")),
			"cancel": (self.close, _("Close Telerising")),
			"red": (self.close, _("Close Telerising")),
			"green": (self.select, _("Run the selected action")),
			"yellow": (self.importChannels, _("Import channels into the TV channel list")),
			"blue": (self.options, _("Open Telerising settings")),
			"info": (self.about, _("About Telerising"))
		}, prio=-1)
		self.onFirstExecBegin.append(self.initialize)

	def initialize(self):
		synchronize(self.session.nav.Scheduler)
		self.refresh()

	def task(self, description, function, callback=None):
		if self.busy:
			return
		self.busy = True
		self["actions"].setEnabled(False)
		self["status"].setText(description)
		deferred = deferToThread(function)

		def complete(value):
			self.busy = False
			self["actions"].setEnabled(True)
			if callback:
				callback(value)
			else:
				self.refresh()

		def failed(failure):
			self.busy = False
			self["actions"].setEnabled(True)
			error = failure.value
			message = str(error) if isinstance(error, (TelerisingError, ReleaseError, ValueError)) else _("Action failed ({error}).").format(error=type(error).__name__)
			self["status"].setText(message)
			self.session.open(MessageBox, message, MessageBox.TYPE_ERROR)

		deferred.addCallbacks(complete, failed)

	def refresh(self):
		self.task(_("Reading status…"), self.manager.status, self.showStatus)

	def showStatus(self, state):
		self.state = state
		active = state["active"]
		version = f"{active['source']} · {active['tag']}" if active else _("Server not installed")
		running = _("Running") if state["ready"] else _("Started, but not reachable") if state["running"] else _("Stopped")
		self["status"].setText(_("{version}\n{status} · Local server · {model}").format(version=version, status=running, model=state["platform"]["model"]))
		self["menu"].setList([
			(_("Install server / check for updates"), self.install),
			(_("Set up provider"), self.setupProvider),
			(_("Import channels into the TV channel list"), self.importChannels),
			(_("EPGImport / program guide"), lambda: epgImport(self.session)),
			(_("Stop server") if state["running"] else _("Start server"), self.toggleService),
			(_("Restart server"), lambda: self.task(_("Restarting server…"), lambda: self.manager.control("restart"))),
			(_("Disable autostart") if state["autostart"] else _("Enable autostart"), self.toggleAutostart),
			(_("Restore previous version"), self.rollback),
			(_("Scheduled tasks"), self.schedules),
			(_("Settings / optional web interface"), self.options)
		])

	def select(self):
		current = self["menu"].getCurrent()
		if current:
			current[1]()

	def about(self):
		text = "\n\n".join([
			_("This helper plugin simplifies Telerising installation, updates, account setup and channel import on Enigma2."),
			_("We cannot provide support for Telerising login or M3U export failures, add providers or extend the server. For these topics, please use the Kodinerds forum."),
			_("Telerising support (Kodinerds):") + "\nhttps://www.kodinerds.net/thread/72127/",
			_("Binary downloads:") + f"\nStandard: https://github.com/{REPOSITORIES['standard']}/tags\nUnofficial: https://github.com/{REPOSITORIES['unofficial']}/tags",
			_("Thanks to easy4me, fds97AVVS and all contributors for Telerising and the available builds.")
		])
		self.session.open(MessageBox, text, type=MessageBox.TYPE_YESNO, typeIcon=MessageBox.TYPE_INFO, list=[(_("OK"), True)], windowTitle=_("About Telerising"), timeout=-1)

	def toggleService(self):
		action = "stop" if self.state.get("running") else "start"
		self.task(_("Stopping server…") if action == "stop" else _("Starting server…"), lambda: self.manager.control(action))

	def toggleAutostart(self):
		self.task(_("Changing autostart…"), lambda: self.manager.autostart(not self.state.get("autostart")))

	def install(self):
		self.task(_("Checking the latest GitHub release…"), self.manager.checkUpdate, self.offerRelease)

	def offerRelease(self, release):
		if not release["update_available"]:
			self.session.open(MessageBox, _("The latest version is already installed."), MessageBox.TYPE_INFO)
			self.refresh()
			return
		message = _("{source}\n{tag}\n\nInstall now? Playing Telerising channels will be interrupted briefly.").format(source=release["source"], tag=release["tag"])
		if self.state.get("platform", {}).get("experimental"):
			message += "\n\n" + _("Dreambox Two: Support for this receiver is experimental.")
		self.session.openWithCallback(lambda answer: self.task(_("Downloading and installing…"), lambda: self.manager.install(release)) if answer else self.refresh(), MessageBox, message, MessageBox.TYPE_YESNO)

	def rollback(self):
		self.session.openWithCallback(lambda answer: self.task(_("Restoring previous version…"), self.manager.rollback) if answer else None, MessageBox, _("Restore the previous server version and keep the current provider profiles?"), MessageBox.TYPE_YESNO)

	def options(self):
		self.session.openWithCallback(self.optionsClosed, Setup, "telerising", plugin="Extensions/Telerising", PluginLanguageDomain=PluginLanguageDomain)

	def optionsClosed(self, *args):
		refreshWebMenu()
		self.refresh()

	def schedules(self):
		self.scheduleBefore = readConfig()["schedule"]
		self.session.openWithCallback(self.schedulesClosed, Setup, "telerisingschedule", plugin="Extensions/Telerising", PluginLanguageDomain=PluginLanguageDomain)

	def schedulesClosed(self, *args):
		if self.scheduleBefore != readConfig()["schedule"]:
			synchronize(self.session.nav.Scheduler, force=True)
		self.refresh()

	def setupProvider(self):
		self.task(_("Loading providers…"), self.manager.providers, lambda catalog: self.chooseProvider(catalog, self.providerForm))

	def chooseProvider(self, catalog, callback):
		choices = [(_("{provider} (configured)").format(provider=metadata.get("name", key)) if metadata.get("configured") else metadata.get("name", key), key, metadata) for key, metadata in sorted(catalog.items(), key=lambda item: (not item[1].get("configured", False), item[1].get("name", item[0]).casefold()))]
		if not choices:
			self.session.open(MessageBox, _("No provider has been configured yet."), MessageBox.TYPE_INFO)
			return
		self.session.openWithCallback(lambda choice: callback(choice[1], choice[2]) if choice else self.refresh(), ChoiceBox, title=_("Telerising provider"), list=choices)

	def providerForm(self, provider, metadata):
		guestAvailable = str(metadata.get("login_required")) == "0"
		if metadata.get("login_type") not in ("username", "email", "country") and not guestAvailable:
			self.session.open(MessageBox, _("This provider requires a device code, cookies or another special login flow. This plugin version supports native username/email login and guest access."), MessageBox.TYPE_INFO)
			return
		if not any(metadata.get("manifest_types", {}).values()):
			return
		self.task(_("Loading provider settings…"), lambda: self.manager.providerSettings(provider), lambda stored: self.session.openWithCallback(lambda *args: self.refresh(), ProviderSetup, self.manager, provider, metadata, stored))

	def importChannels(self):
		self.task(_("Loading configured providers…"), self.manager.configured, lambda catalog: self.chooseProvider(catalog, lambda provider, metadata: self.task(_("Fetching channel list…"), lambda: self.manager.preview(provider), self.offerImport)))

	def offerImport(self, preview):
		message = _("Import {count} channels into a dedicated TV bouquet?\n\n{channels}\n\nAn earlier Telerising import for this provider will be backed up and replaced.").format(count=preview["count"], channels="\n".join(preview["names"][:6]))
		message += "\n\n" + _("EPG/picon references matched: {matched}/{count}. Select sources and import guide data in EPGImport.").format(matched=preview["matched"], count=preview["count"])

		def confirmed(answer):
			if answer:
				self.task(_("Creating TV bouquet…"), lambda: self.manager.importBouquet(preview), self.importComplete)
		self.session.openWithCallback(confirmed, MessageBox, message, MessageBox.TYPE_YESNO)

	def importComplete(self, result):
		eDVBDB.getInstance().reloadBouquets()
		self.session.open(MessageBox, _("Imported {count} channels. The Telerising bouquet is now available in the TV channel list.").format(count=result["channels"]), MessageBox.TYPE_INFO)
		self.refresh()


def main(session, **kwargs):
	session.open(TelerisingSetup)


def menu(menuid, **kwargs):
	return [("Telerising", main, "telerising", 70)] if menuid == "network" else []


def sessionStart(reason, session=None, **kwargs):
	if reason == 0 and session:
		setupWeb()
		deferToThread(Manager().restoreSettings, reload=True).addErrback(lambda failure: print(f"[Telerising] Could not restore service settings: {failure.type.__name__}"))
		synchronize(session.nav.Scheduler)


def Plugins(**kwargs):
	register()
	setOnSaveCallback("telerising", refreshWebMenu)
	# Also register when the plugin list is refreshed in an existing session.
	reactor.callLater(0, setupWeb)
	return [
		PluginDescriptor(name="Telerising", description=_("Local TV server and channel import"), where=PluginDescriptor.WHERE_PLUGINMENU, icon="pluginfhd.png" if getDesktop(0).size().width() >= 1920 else "plugin.png", fnc=main, needsRestart=False),
		PluginDescriptor(name="Telerising", description=_("Local TV server"), where=PluginDescriptor.WHERE_MENU, fnc=menu, needsRestart=False),
		PluginDescriptor(where=PluginDescriptor.WHERE_SESSIONSTART, fnc=sessionStart, needsRestart=False, weight=101)
	]
