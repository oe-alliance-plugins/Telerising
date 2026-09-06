from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from enigma import eDVBDB
from Scheduler import AFTEREVENT, SchedulerEntry, TIMERTYPE, addFunctionTimer, functionTimers
from Screens.Toast import Toast
from twisted.internet.threads import deferToThread

from . import _
from .core import Manager, readConfig
from .settings import settings

FUNCTIONS = {"update": "telerising-update-check", "channels": "telerising-channel-sync"}


class ScheduledOperation:
	def __init__(self, kind):
		self.kind = kind
		self.cancelled = Event()
		self.running = False

	def start(self, callback, entry):
		if self.running:
			return False
		self.running = True
		self.cancelled.clear()
		manager = Manager()
		result = {"bouquets": 0}

		def work():
			if self.kind == "update":
				release = manager.checkUpdate()
				if not self.cancelled.is_set():
					manager.recordJob("update", {"success": True, "available": release["update_available"], "source": release["source"], "tag": release["tag"]})
					result["release"] = release
					result["notify"] = readConfig()["jobs"].get(f"notified_{release['source']}", {}).get("tag") != release["tag"]
				return 0
			config = readConfig()
			if not config["active"] or not manager.status()["running"]:
				return 0
			count = 0
			for provider, imported in config["imports"].items():
				if self.cancelled.is_set():
					break
				# Synchronize only existing, previously imported bouquets of this source.
				if imported["source"] != config["active"]["source"] or not (Path("/etc/enigma2") / f"userbouquet.telerising_{provider}.tv").is_file():
					continue
				preview = manager.preview(provider)
				if self.cancelled.is_set():
					break
				manager.importBouquet(preview)
				count += 1
				result["bouquets"] = count
			if not self.cancelled.is_set():
				manager.recordJob("channels", {"success": True, "bouquets": count})
			return count

		def completed(count):
			self.running = False
			if count:
				eDVBDB.getInstance().reloadBouquets()
			if not self.cancelled.is_set():
				release = result.get("release")
				if release and release["update_available"] and result.get("notify") and settings.update_toast.value and settings.source.value == release["source"] and Toast.instance:
					Toast.instance.showToast(_("Telerising update available: {source} {tag}").format(source=release["source"], tag=release["tag"]), Toast.TYPE_INFO, timeout=6)
					deferToThread(manager.recordJob, f"notified_{release['source']}", {"tag": release["tag"]}).addErrback(lambda failure: entry.log(30, _("Could not save the update notification state.")))
				entry.log(0, _("Telerising scheduled task completed."))
				callback(True)
			else:
				entry.functionRunning = False
				entry.functionFinished = True

		def failed(failure):
			self.running = False
			if result["bouquets"]:
				eDVBDB.getInstance().reloadBouquets()
			if not self.cancelled.is_set():
				entry.log(30, _("Telerising scheduled task failed ({error}).").format(error=type(failure.value).__name__))
				callback(False)
			else:
				entry.functionRunning = False
				entry.functionFinished = True

		deferToThread(work).addCallback(completed).addErrback(failed)
		return True

	def cancel(self):
		self.cancelled.set()


operations = {kind: ScheduledOperation(kind) for kind in FUNCTIONS}


def register():
	names = {"update": _("Telerising: Check for updates"), "channels": _("Telerising: Synchronize channels")}
	for kind, key in FUNCTIONS.items():
		if not functionTimers.getItem(key):
			addFunctionTimer(key, names[kind], operations[kind].start, operations[kind].cancel, useOwnThread=True)


def synchronize(scheduler, force=False):
	register()
	settings = readConfig()["schedule"]
	for kind, key in FUNCTIONS.items():
		existing = [entry for entry in scheduler.timer_list + scheduler.processed_timers if entry.timerId == key]
		interval = settings[f"{kind}_interval"]
		if existing and interval != "off" and not force:
			continue
		for entry in existing:
			scheduler.removeEntry(entry)
		if interval == "off":
			continue
		now = datetime.now()
		hour, minute = settings[f"{kind}_time"]
		date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
		if interval == "weekly":
			date += timedelta(days=(settings[f"{kind}_day"] - date.weekday()) % 7)
		if date <= now:
			date += timedelta(days=7 if interval == "weekly" else 1)
		begin = int(date.timestamp())
		entry = SchedulerEntry(begin, begin + 3600, timerType=TIMERTYPE.OTHER, afterEvent=AFTEREVENT.NONE)
		entry.timerId = key
		entry.function = key
		entry.functionStandby = 1 if settings["standby_only"] else 0
		entry.functionStandbyRetry = bool(settings["standby_only"])
		entry.functionRetryCount = 0
		for day in range(7) if interval == "daily" else [settings[f"{kind}_day"]]:
			entry.setRepeated(day)
		scheduler.record(entry)
