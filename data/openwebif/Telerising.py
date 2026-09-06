from Plugins.Extensions.Telerising import __version__
from Plugins.Extensions.Telerising.core import readConfig
from Plugins.Extensions.Telerising.webif import TelerisingResource
from Plugins.Extensions.WebInterface.WebChilds.Toplevel import addExternalChild

addExternalChild(("telerising", TelerisingResource(), "Telerising", __version__, readConfig()["webif"], "_self"))
