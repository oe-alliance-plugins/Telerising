from datetime import datetime
from Components.config import ConfigClock, ConfigPassword, ConfigSelection, ConfigSubsection, ConfigText, ConfigYesNo, NoSave, config

from . import _

config.plugins.telerising = ConfigSubsection()
settings = config.plugins.telerising
settings.source = ConfigSelection(default="standard", choices=[("standard", "Standard"), ("unofficial", "Unofficial")])
settings.webif = ConfigYesNo(default=False)
settings.autostart = ConfigYesNo(default=False)
settings.service_type = ConfigSelection(default="5001", choices=[("5001", "ServiceApp / gstplayer (5001)"), ("5002", "ServiceApp / exteplayer3 (5002)"), ("4097", _("Enigma2 / internal GStreamer (4097)"))])
settings.update_toast = ConfigYesNo(default=True)
intervals = [("off", _("Disabled")), ("daily", _("Daily")), ("weekly", _("Weekly"))]
weekdays = [(str(day), label) for day, label in enumerate([_("Monday"), _("Tuesday"), _("Wednesday"), _("Thursday"), _("Friday"), _("Saturday"), _("Sunday")])]
settings.update_interval = ConfigSelection(default="weekly", choices=intervals)
settings.update_time = ConfigClock(default=int(datetime.now().replace(hour=4, minute=0, second=0).timestamp()))
settings.update_day = ConfigSelection(default="0", choices=weekdays)
settings.channels_interval = ConfigSelection(default="daily", choices=intervals)
settings.channels_time = ConfigClock(default=int(datetime.now().replace(hour=4, minute=15, second=0).timestamp()))
settings.channels_day = ConfigSelection(default="0", choices=weekdays)
settings.standby_only = ConfigYesNo(default=False)

# The local server stores account credentials; the E2 form does not save them.
settings.provider = ConfigSubsection()
settings.provider.country_mode = NoSave(ConfigYesNo(default=False))
settings.provider.country = NoSave(ConfigSelection(default="de", choices=[("de", _("Germany"))]))
settings.provider.login = NoSave(ConfigText(default="", fixed_size=False))
settings.provider.password = NoSave(ConfigPassword(default="", fixed_size=False))
settings.provider.guest = NoSave(ConfigYesNo(default=False))
settings.provider.guest_available = NoSave(ConfigYesNo(default=False))
settings.provider.manifest = NoSave(ConfigSelection(default="dash", choices=[("dash", "DASH")]))
settings.provider.bandwidth = NoSave(ConfigSelection(default="3000", choices=[("3000", "720p25")]))
