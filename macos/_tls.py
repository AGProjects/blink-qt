from sipsimple.configuration.settings import TLSSettings
from sipsimple.configuration import Setting
from blink.configuration.datatypes import ApplicationDataPath
from blink.resources import Resources

class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=ApplicationDataPath, default=ApplicationDataPath(Resources.get('tls/ca.crt')), nillable=True)
    certificate = Setting(type=ApplicationDataPath, default=ApplicationDataPath(Resources.get('tls/default.crt')), nillable=True)
    verify_server = Setting(type=bool, default=False)

