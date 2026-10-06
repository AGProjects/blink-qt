
import os
import sys
import signal
import socket
import platform
import time

from threading import Thread

# QtWebEngine's embedded Chromium spams stderr with harmless errors on systems
# where it cannot export GPU buffers to a dma_buf (e.g. Raspberry Pi and other
# ARM/GBM setups): "gbm_wrapper.cc ... Failed to export buffer to dma_buf". The
# web views fall back to shared-memory buffers and render correctly, so we just
# disable Chromium's logging to keep the console clean. Any flags the user sets
# via QTWEBENGINE_CHROMIUM_FLAGS are preserved. This must run before QtWebEngine
# is imported/initialized below.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = ("--disable-logging " + os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")).strip()

from PyQt6.QtCore import Qt, QEvent, QLocale, QTranslator, QLoggingCategory, QSocketNotifier
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtGui import QIcon

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import host, makedirs
from eventlib import api
from zope.interface import implementer

from sipsimple import __version__ as sdk_version
from sipsimple.application import SIPApplication
from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.addressbook import Contact, Group
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.configuration.backend.file import FileBackend
from sipsimple.payloads import XMLDocument
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread

from blink.__info__ import __project__, __summary__, __webpage__, __version__, __date__, __author__, __email__, __license__, __copyright__

try:
    from blink import branding
except ImportError:
    branding = Null

from blink.chatwindow import ChatWindow
from blink.logswindow import LogsWindow
from blink.configuration.account import AccountExtension, BonjourAccountExtension
from blink.configuration.addressbook import ContactExtension, GroupExtension
from blink.configuration.settings import SIPSimpleSettingsExtension
from blink.logging import ActivityLog, LogManager
from blink.mainwindow import MainWindow
from blink.presence import PresenceManager
from blink.resources import ApplicationData, Resources
from blink.sessions import SessionManager
from blink.update import UpdateManager
from blink.util import QSingleton, run_in_gui_thread


__all__ = ['Blink']

# Handle high resolution displays, can be removed for QT6:
if hasattr(Qt, 'AA_EnableHighDpiScaling'):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

if hasattr(sys, 'frozen'):
    import httplib2
    httplib2.CA_CERTS = os.environ['SSL_CERT_FILE'] = Resources.get('tls/cacerts.pem')
    makedirs(ApplicationData.get('logs'))
    sys.stdout.file = ApplicationData.get('logs/output.log')

if platform.system() == 'Darwin':
    QApplication.setStyle('Fusion')
    os.environ["QMLSCENE_DEVICE"] = "softwarecontext"

web_logging = QLoggingCategory('qt.webengine')
web_logging.setFilterRules('*.info=false')

class IPAddressMonitor(object):
    """
    An object which monitors the IP address used for the default route of the
    host and posts a SystemIPAddressDidChange notification when a change is
    detected.
    """

    def __init__(self):
        self.greenlet = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()

        if self.greenlet is not None:
            return
        self.greenlet = api.getcurrent()

        current_address = host.default_ip
        while True:
            new_address = host.default_ip
            # make sure the address stabilized
            api.sleep(5)
            if new_address != host.default_ip:
                continue
            if new_address != current_address:
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=NotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None


@implementer(IObserver)
class Blink(QApplication, metaclass=QSingleton):

    # Notifications logged to the Activity log, matching what Blink for macOS logs
    __activity_notifications__ = ('SIPAccountManagerWillStart', 'SIPAccountDidActivate', 'SIPAccountDidDeactivate',
                                  'SIPAccountRegistrationDidSucceed', 'SIPAccountRegistrationDidFail', 'SIPAccountRegistrationDidEnd',
                                  'SIPAccountRegistrationGotAnswer',
                                  'TLSTransportHasChanged', 'XCAPManagerDidDiscoverServerCapabilities', 'XCAPManagerClientError',
                                  'SystemIPAddressDidChange')

    def __init__(self):
        super(Blink, self).__init__(sys.argv)
        self._log_versions()
        self.registrar_addresses = {}
        self._tls_diagnosed = {}
        self._registrar_lookups = {}     # DNSLookup -> account, for logging where a failed registration was sent
        self._registrar_logged = {}      # account id -> (monotonic time, routes) of the last logged lookup
        self.contact_addresses = {}
        self.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
        self.sip_application = SIPApplication()
        self.first_run = False
        self.reinit = False
        self.quitting = False
        self._pending_signal = None

        translator = QTranslator(self)
        system_language = QLocale.system().name().split('_')[0]
        language = system_language
        if os.path.exists(ApplicationData.get('config')):
            pre_loaded_settings = FileBackend(ApplicationData.get('config')).load()
            try:
                language = pre_loaded_settings['BlinkSettings']['interface']['language']
            except KeyError:
                pass
            if language == 'default':
                language = system_language
            if translator.load(Resources.get(f'i18n/blink_{language}')):
                self.installTranslator(translator)

        self.setOrganizationDomain("ag-projects.com")
        self.setOrganizationName("AG Projects")
        self.setApplicationName("Blink")
        self.setApplicationVersion(__version__)
        self.setWindowIcon(QIcon(Resources.get('icons/blink.png')))

        self.main_window = MainWindow()
        self.chat_window = ChatWindow()
        self.logs_window = LogsWindow()
        self.main_window.__closed__ = True
        self.chat_window.__closed__ = True
        self.main_window.installEventFilter(self)
        self.chat_window.installEventFilter(self)
        self.logs_window.installEventFilter(self)

        self.main_window.addAction(self.chat_window.control_button.actions.main_window)
        self.chat_window.addAction(self.main_window.quit_action)
        self.chat_window.addAction(self.main_window.help_action)
        self.chat_window.addAction(self.main_window.redial_action)
        self.chat_window.addAction(self.main_window.join_conference_action)
        self.chat_window.addAction(self.main_window.mute_action)
        self.chat_window.addAction(self.main_window.silent_action)
        self.chat_window.addAction(self.main_window.preferences_action)
        self.chat_window.addAction(self.main_window.transfers_window_action)
        self.chat_window.addAction(self.main_window.logs_window_action)
        self.chat_window.addAction(self.main_window.received_files_window_action)
        self.chat_window.addAction(self.main_window.screenshots_window_action)

        self.ip_address_monitor = IPAddressMonitor()
        self.log_manager = LogManager()
        self.presence_manager = PresenceManager()
        self.session_manager = SessionManager()
        self.update_manager = UpdateManager()

        # Prevent application from exiting after last window is closed if system tray was initialized
        if self.main_window.system_tray_icon:
            self.setQuitOnLastWindowClosed(False)

        self.main_window.check_for_updates_action.triggered.connect(self.update_manager.check_for_updates)
        self.main_window.check_for_updates_action.setVisible(self.update_manager != Null)

        if getattr(sys, 'frozen', False):
            XMLDocument.schema_path = Resources.get('xml-schemas')

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        Contact.register_extension(ContactExtension)
        Group.register_extension(GroupExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.sip_application)
        for name in self.__activity_notifications__:
            notification_center.add_observer(self, name=name)

        branding.setup(self)

    def run(self):
        self.first_run = not os.path.exists(ApplicationData.get('config'))
        self._install_signal_handlers()
        self.sip_application.start(FileStorage(ApplicationData.directory))
        self.exec()
        self.quitting = True
        activity = ActivityLog()
        activity.info('User interface closed, stopping the SIP application')
        self.update_manager.shutdown()
        self.sip_application.stop()
        self.sip_application.thread.join()
        activity.info('SIP application stopped')
        self.log_manager.stop()
        if self.reinit:
            os.execl(sys.executable, sys.executable, *sys.argv)

    def _install_signal_handlers(self):
        # Python only runs signal handlers when the interpreter gets control,
        # which may not happen while Qt sits in its event loop. The wakeup fd
        # makes the event loop notice the signal immediately.
        self._signal_rsock, self._signal_wsock = socket.socketpair()
        self._signal_rsock.setblocking(False)
        self._signal_wsock.setblocking(False)
        signal.set_wakeup_fd(self._signal_wsock.fileno())
        self._signal_notifier = QSocketNotifier(self._signal_rsock.fileno(), QSocketNotifier.Type.Read, self)
        self._signal_notifier.activated.connect(self._SH_SignalWakeup)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        name = signal.Signals(signum).name
        if self.quitting:
            # second signal while shutting down (e.g. the SIP stack waits for
            # unregistration to time out): give up waiting
            ActivityLog().warning('Received %s during shutdown, exiting immediately' % name)
            os._exit(1)
        # quit from the event loop (see _SH_SignalWakeup), not from inside
        # whatever code the handler interrupted
        self._pending_signal = name

    def _SH_SignalWakeup(self):
        try:
            while self._signal_rsock.recv(64):
                pass
        except BlockingIOError:
            pass
        name, self._pending_signal = self._pending_signal, None
        if name is not None and not self.quitting:
            ActivityLog().info('Received %s, quitting' % name)
            self.quitting = True
            self.quit()

    def quit(self):
        ActivityLog().info('Quit requested')
        self.chat_window.close()
        self.main_window.close()
        super(Blink, self).quit()

    def restart(self):
        self.reinit = True
        self.quit()

    @property
    def available_codecs(self):
        if SIPApplication and SIPApplication.engine and SIPApplication.engine._ua:
            return list(codec.decode() for codec in SIPApplication.engine._ua.available_codecs)
        else:
            return []

    @property
    def available_video_codecs(self):
        if SIPApplication and SIPApplication.engine and SIPApplication.engine._ua:
            return list(codec.decode() for codec in SIPApplication.engine._ua.available_video_codecs)
        else:
            return []

    def _log_versions(self):
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        activity = ActivityLog()
        activity.info('Starting Blink %s (%s)' % (__version__, __date__))
        activity.info('Running on %s %s (%s), Python %s, PyQt %s, Qt %s' % (platform.system(), platform.release(), platform.machine(), platform.python_version(), PYQT_VERSION_STR, QT_VERSION_STR))
        try:
            from sipsimple.core import CORE_REVISION, PJ_VERSION, PJ_SVN_REVISION
        except ImportError:
            activity.info('Using SIP SIMPLE SDK version %s' % sdk_version)
        else:
            pj_version = PJ_VERSION.decode() if isinstance(PJ_VERSION, bytes) else PJ_VERSION
            activity.info('Using SIP SIMPLE SDK version %s, core version %s, PJSIP version %s (rev %s)' % (sdk_version, CORE_REVISION, pj_version, PJ_SVN_REVISION))
        activity.info('Data directory: %s' % ApplicationData.directory)

    def _log_media(self):
        settings = SIPSimpleSettings()
        activity = ActivityLog()
        activity.info('SIP device ID: %s' % settings.instance_id)
        activity.info('Core audio codecs: %s' % ', '.join(self.available_codecs))
        activity.info('Configured audio codecs: %s' % ', '.join(settings.rtp.audio_codec_list))
        activity.info('Core video codecs: %s' % ', '.join(self.available_video_codecs))
        activity.info('Configured video codecs: %s' % ', '.join(settings.rtp.video_codec_list))
        activity.info('Audio devices: input %s, output %s, alert %s' % (settings.audio.input_device, settings.audio.output_device, settings.audio.alert_device))
        engine = SIPApplication.engine
        try:
            video_devices = [device for device in engine.video_devices if device not in ('system_default', None)]
        except Exception:
            video_devices = []
        if video_devices:
            activity.info('Available video cameras: %s' % ', '.join(video_devices))
        activity.info('Using video camera %s' % settings.video.device)

    def eventFilter(self, watched, event):
        if watched in (self.main_window, self.chat_window):
            if event.type() == QEvent.Type.Show:
                watched.__closed__ = False
            elif event.type() == QEvent.Type.Close:
                watched.__closed__ = True
                if self.main_window.__closed__ and self.chat_window.__closed__:
                    # close auxiliary windows
                    self.main_window.conference_dialog.close()
                    self.main_window.filetransfer_window.close()
                    self.main_window.preferences_window.close()
        if watched is self.chat_window:
            if event.type() == QEvent.Type.WindowActivate:
                #self.main_window.hide_new_messages_label()
                try:
                    watched.confirm_read_messages(watched.selected_session)
                except KeyError:
                    pass
        return False

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_CallFunctionEvent(self, event):
        try:
            event.function(*event.args, **event.kw)
        except:
            log.exception('Exception occurred while calling function %s in the GUI thread' % event.function.__name__)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        self.log_manager.start()
        self.presence_manager.start()

    @run_in_gui_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self.ip_address_monitor.start()
        self.main_window.show()
        accounts = AccountManager().get_accounts()
        if not accounts or (self.first_run and accounts == [BonjourAccount()]):
            self.main_window.preferences_window.show_create_account_dialog()
        self.update_manager.initialize()
        self._log_media()
        msg = 'Available audio codecs: %s\n' % ", ".join(self.available_codecs)
        NotificationCenter().post_notification('UILogMessage', data=NotificationData(message=msg, section='sip'))
        msg = 'Available video codecs: %s\n' % ", ".join(self.available_video_codecs)
        NotificationCenter().post_notification('UILogMessage', data=NotificationData(message=msg, section='sip'))

    def _NH_SIPApplicationWillEnd(self, notification):
        ActivityLog().info('Stopping Blink')
        self.ip_address_monitor.stop()

    def _NH_SIPAccountManagerWillStart(self, notification):
        if getattr(notification.data, 'bonjour_available', False):
            ActivityLog().info('Bonjour discovery is available')
        else:
            ActivityLog().info('Bonjour discovery is not available')

    def _NH_SIPAccountDidActivate(self, notification):
        ActivityLog().info('Account %s activated' % notification.sender.id)

    def _NH_SIPAccountDidDeactivate(self, notification):
        ActivityLog().info('Account %s deactivated' % notification.sender.id)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        data = notification.data
        address = '%s:%s;transport=%s' % (data.registrar.address, data.registrar.port, data.registrar.transport)
        contact = str(data.contact_header.uri)
        registrar_changed = self.registrar_addresses.get(account.id) != address
        contact_changed = self.contact_addresses.get(account.id) != contact
        if registrar_changed and contact_changed:
            ActivityLog().info('Account %s registered contact %s at %s for %d seconds' % (account.id, contact, address, data.expires))
        elif contact_changed:
            ActivityLog().debug('Account %s changed contact to %s' % (account.id, contact))
        elif registrar_changed:
            ActivityLog().debug('Account %s changed registrar to %s' % (account.id, address))
        self.registrar_addresses[account.id] = address
        self.contact_addresses[account.id] = contact
        if account.contact.public_gruu is not None:
            ActivityLog().debug('Account %s has public SIP GRUU %s' % (account.id, account.contact.public_gruu))

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        account = notification.sender
        error = notification.data.error
        error = error.decode(errors='replace') if isinstance(error, bytes) else str(error)
        ActivityLog().warning('Account %s failed to register: %s' % (account.id, error))
        self._log_registrar_routes(account)
        if 'ECERTVERIF' in error or 'certificate' in error.lower():
            self._diagnose_tls(account)

    def _NH_SIPAccountRegistrationGotAnswer(self, notification):
        # one answer per registrar tried, when the SDK reports them
        data = notification.data
        registrar = getattr(data, 'registrar', None)
        code = getattr(data, 'code', None)
        if registrar is None or code is None or 200 <= code < 300:
            return
        reason = data.reason.decode(errors='replace') if isinstance(getattr(data, 'reason', None), bytes) else getattr(data, 'reason', '')
        ActivityLog().warning('Account %s registrar %s:%s;transport=%s answered %s %s' % (notification.sender.id, registrar.address, registrar.port, registrar.transport, code, reason))

    def _log_registrar_routes(self, account):
        """Log where the registration goes: the same lookup the SDK makes (outbound proxy, or the
        domain's NAPTR/SRV/A records), in the order the registrars are tried."""
        from sipsimple.core import SIPURI
        from sipsimple.lookup import DNSLookup
        proxy = account.sip.outbound_proxy
        if proxy is not None:
            uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            source = 'outbound proxy %s' % proxy.host
        else:
            uri = SIPURI(host=account.id.domain)
            source = 'DNS of %s' % account.id.domain
        lookup = DNSLookup()
        self._registrar_lookups[lookup] = (account, source)
        NotificationCenter().add_observer(self, sender=lookup)
        lookup.lookup_sip_proxy(uri, SIPSimpleSettings().sip.transport_list, tls_name=account.sip.tls_name or uri.host)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        account, source = self._registrar_lookups.pop(notification.sender, (None, None))
        if account is None:
            return
        routes = ', '.join('%s:%s;transport=%s' % (route.address, route.port, route.transport) for route in notification.data.result)
        now = time.monotonic()
        last_time, last_routes = self._registrar_logged.get(account.id, (None, None))
        if last_routes == routes and now - last_time < 600:
            return  # same destinations as the last failure, said once every 10 minutes
        self._registrar_logged[account.id] = (now, routes)
        ActivityLog().warning('Account %s registers through %s: %s (tried in this order)' % (account.id, source, routes or 'no destinations'))

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        account, source = self._registrar_lookups.pop(notification.sender, (None, None))
        if account is not None:
            ActivityLog().warning('Account %s cannot find its registrar (%s): %s' % (account.id, source, notification.data.error))

    tls_diagnosis_interval = 300  # seconds between certificate checks of one account

    def _diagnose_tls(self, account):
        """Find out why a TLS certificate was refused: PJSIP only says that it was.

        The server is contacted again from Python with the same CA list and server
        name, and the refusal reason (unknown issuer, name mismatch, expired) is
        logged with who issued the certificate, for whom and until when.
        """
        now = time.monotonic()
        if now - self._tls_diagnosed.get(account.id, -self.tls_diagnosis_interval) < self.tls_diagnosis_interval:
            return
        self._tls_diagnosed[account.id] = now
        settings = SIPSimpleSettings()
        ca_file = settings.tls.ca_list.normalized if settings.tls.ca_list is not None else None
        server_name = account.sip.tls_name or account.id.domain
        targets = []
        registrar = self.registrar_addresses.get(account.id)
        if registrar and registrar.endswith('transport=tls'):
            host, _, port = registrar.partition(';')[0].rpartition(':')
            targets.append((host, int(port)))
        proxy = account.sip.outbound_proxy
        if proxy is not None and proxy.transport == 'tls':
            targets.append((proxy.host, proxy.port or 5061))
        if not targets:
            targets.append((account.id.domain, 5061))
        Thread(target=self._check_tls_certificate, args=(account.id, targets[0], server_name, ca_file), name='tls-check', daemon=True).start()

    @staticmethod
    def _check_tls_certificate(account_id, target, server_name, ca_file):
        """Check every address of the server: the chain against the CA list, and the
        certificate's name against the TLS name and against the host name."""
        import ssl
        from gnutls.crypto import X509Certificate
        activity = ActivityLog()
        host, port = target
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError as e:
            activity.warning('[tls] Cannot resolve %s for account %s: %s' % (host, account_id, e))
            return

        def attempt(address, name, check_name, cafile=ca_file):
            context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
            context.check_hostname = check_name
            try:
                with socket.create_connection((address, port), timeout=10) as raw_socket:
                    with context.wrap_socket(raw_socket, server_hostname=name) as tls_socket:
                        tls_socket.getpeercert(binary_form=True)
            except ssl.SSLCertVerificationError as e:
                return e.verify_message or str(e)
            except (OSError, ssl.SSLError) as e:
                return 'cannot connect: %s' % e
            return 'ok'

        def describe(address):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                with socket.create_connection((address, port), timeout=10) as raw_socket:
                    with context.wrap_socket(raw_socket, server_hostname=server_name) as tls_socket:
                        der = tls_socket.getpeercert(binary_form=True)
                certificate = X509Certificate(ssl.DER_cert_to_PEM_cert(der).encode())
                until = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(certificate.expiration_time))
                alt_names = ', '.join(str(name) for name in getattr(certificate, 'alternative_names', None).dns) if getattr(certificate, 'alternative_names', None) else ''
                return '%s%s, issued by %s, valid until %s' % (certificate.subject, (' (also %s)' % alt_names) if alt_names else '', certificate.issuer, until)
            except Exception as e:
                return 'certificate unreadable: %s' % e

        names = [server_name] + ([host] if host != server_name else [])
        refused = False
        for address in addresses:
            chain = attempt(address, server_name, False)
            checks = ['chain %s' % chain] + ['name %s %s' % (name, attempt(address, name, True) if chain == 'ok' else 'not checked') for name in names]
            if chain != 'ok' and ca_file:
                checks.append('system CA store %s' % attempt(address, server_name, False, cafile=None))
            refused = refused or any(not check.endswith(' ok') for check in checks[:2])
            activity.info('[tls] %s %s:%d for account %s: %s; %s' % (host, address, port, account_id, ', '.join(checks), describe(address)))
        if not refused:
            activity.info('[tls] Every address of %s verifies here with %s and name %s; the SIP stack checks something else (see the PJSIP trace)'
                          % (host, ca_file or 'the system CA store', server_name))

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        account = notification.sender
        ActivityLog().info('Account %s was unregistered' % account.id)
        self.registrar_addresses.pop(account.id, None)
        self.contact_addresses.pop(account.id, None)

    def _NH_TLSTransportHasChanged(self, notification):
        data = notification.data
        ActivityLog().info('TLS transport verify server: %s' % data.verify_server)
        ActivityLog().info('TLS transport certificate: %s' % data.certificate)
        ActivityLog().info('TLS transport authorities: %s' % data.ca_file)

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        manager = notification.sender
        if manager.xcap_root is None:
            return
        ActivityLog().debug('Using XCAP root %s for account %s' % (manager.xcap_root, manager.account.id))
        ActivityLog().debug('XCAP server capabilities: %s' % ', '.join(notification.data.auids))

    def _NH_XCAPManagerClientError(self, notification):
        manager = notification.sender
        ActivityLog().error('XCAP error for account %s (%s): %s' % (manager.account.id, manager.xcap_root, notification.data.error))

    def _NH_SystemIPAddressDidChange(self, notification):
        ActivityLog().info('IP address changed from %s to %s' % (notification.data.old_ip_address, notification.data.new_ip_address))

    def _NH_SIPApplicationDidEnd(self, notification):
        self.presence_manager.stop()

    @run_in_gui_thread
    def _NH_SIPApplicationGotFatalError(self, notification):
        log.error('Fatal error:\n{}'.format(notification.data.traceback))
        ActivityLog().error('Fatal error:\n%s' % notification.data.traceback)
        QMessageBox.critical(self.main_window, "Fatal Error", "A fatal error occurred, {} will now exit.".format(self.applicationName()))
        sys.exit(1)
