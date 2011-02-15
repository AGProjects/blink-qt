# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']


__version__ = '0.2.4'
__date__    = 'February 15th, 2010'


import os
import sys
from collections import defaultdict

import cjson
from PyQt4.QtGui import QApplication
from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from application.system import host, unlink
from eventlet import api
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileBackend
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData, makedirs

from blink.configuration.account import AccountExtension, BonjourAccountExtension
from blink.configuration.datatypes import InvalidToken
from blink.configuration.settings import SIPSimpleSettingsExtension
from blink.logging import LogManager
from blink.mainwindow import MainWindow
from blink.resources import ApplicationData
from blink.sessions import SessionManager
from blink.update import UpdateManager
from blink.util import QSingleton, run_in_gui_thread


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
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=TimestampedNotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None


class Blink(QApplication):
    __metaclass__ = QSingleton

    implements(IObserver)

    def __init__(self):
        super(Blink, self).__init__(sys.argv)
        self.application = SIPApplication()
        self.first_run = False
        self.main_window = MainWindow()
        self.ip_address_monitor = IPAddressMonitor()

        self.update_manager = UpdateManager()
        self.main_window.check_for_updates_action.triggered.connect(self.update_manager.check_for_updates)
        self.main_window.check_for_updates_action.setVisible(self.update_manager != Null)

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)
        session_manager = SessionManager()
        session_manager.initialize(self.main_window, self.main_window.session_model)

    def run(self):
        from blink.util import call_in_gui_thread as call_later
        call_later(self._initialize_sipsimple) # initialize sipsimple after the qt event loop is started
        self.exec_()
        self.update_manager.shutdown()
        self.application.stop()
        self.application.thread.join()
        log_manager = LogManager()
        log_manager.stop()

    def fetch_account(self):
        filename = os.path.expanduser('~/.blink_account')
        if not os.path.exists(filename):
            return
        try:
            data = open(filename).read()
            data = cjson.decode(data.replace(r'\/', '/'))
        except (OSError, IOError), e:
            print "Failed to read json data from ~/.blink_account: %s" % e
            return
        except cjson.DecodeError, e:
            print "Failed to decode json data from ~/.blink_account: %s" % e
            return
        finally:
            unlink(filename)
        data = defaultdict(lambda: None, data)
        account_id = data['sip_address']
        if account_id is None:
            return
        account_manager = AccountManager()
        try:
            account = account_manager.get_account(account_id)
        except KeyError:
            account = Account(account_id)
            account.display_name = data['display_name'].decode('utf-8')
            default_account = account
        else:
            default_account = account_manager.default_account
        account.auth.username = data['auth_username']
        account.auth.password = data['password'] or ''
        account.sip.outbound_proxy = data['outbound_proxy']
        account.xcap.xcap_root = data['xcap_root']
        account.nat_traversal.msrp_relay = data['msrp_relay']
        account.server.settings_url = data['settings_url']
        if data['passport'] is not None:
            try:
                passport = data['passport']
                certificate_path = self.save_certificates(account_id, passport['crt'], passport['key'], passport['ca'])
                account.tls.certificate = certificate_path
            except (GNUTLSError, IOError, OSError):
                pass
        account.enabled = True
        account.save()
        account_manager.default_account = default_account

    def save_certificates(self, sip_address, crt, key, ca):
        crt = crt.strip() + os.linesep
        key = key.strip() + os.linesep
        ca = ca.strip() + os.linesep
        X509Certificate(crt)
        X509PrivateKey(key)
        X509Certificate(ca)
        makedirs(ApplicationData.get('tls'))
        certificate_path = ApplicationData.get(os.path.join('tls', sip_address+'.crt'))
        file = open(certificate_path, 'w')
        os.chmod(certificate_path, 0600)
        file.write(crt+key)
        file.close()
        ca_path = ApplicationData.get(os.path.join('tls', 'ca.crt'))
        try:
            existing_cas = open(ca_path).read().strip() + os.linesep
        except:
            file = open(ca_path, 'w')
            file.write(ca)
            file.close()
        else:
            if ca not in existing_cas:
                file = open(ca_path, 'w')
                file.write(existing_cas+ca)
                file.close()
        settings = SIPSimpleSettings()
        settings.tls.ca_list = ca_path
        settings.save()
        return certificate_path

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_CallFunctionEvent(self, event):
        try:
            event.function(*event.args, **event.kw)
        except:
            log.error('Exception occured while calling function %s in the GUI thread' % event.function.__name__)
            log.err()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        log_manager = LogManager()
        log_manager.start()

    @run_in_gui_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self.ip_address_monitor.start()
        self.fetch_account()
        self.main_window.show()
        settings = SIPSimpleSettings()
        accounts = AccountManager().get_accounts()
        if not accounts or (self.first_run and accounts==[BonjourAccount()]):
            self.main_window.preferences_window.show_create_account_dialog()
        if settings.google_contacts.authorization_token is InvalidToken:
            self.main_window.google_contacts_dialog.open_for_incorrect_password()
        self.update_manager.initialize()

    def _NH_SIPApplicationWillEnd(self, notification):
        self.ip_address_monitor.stop()

    def _initialize_sipsimple(self):
        if not os.path.exists(ApplicationData.get('config')):
            self.first_run = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.application)
        self.application.start(FileBackend(ApplicationData.get('config')))


