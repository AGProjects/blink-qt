"""Account server tools: the account's web pages on its SIP server (settings, search for people,
history), shown in a QtWebEngine window. Imported only where QtWebEngine is available; builds
without it (Windows) have no server tools."""

import os
import sys

from PyQt6 import sip
from PyQt6.QtCore import Qt, QUrl, QUrlQuery
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QAuthenticator
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMenu

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from blink.configuration.settings import BlinkSettings
from blink.contacts import URIUtils
from blink.resources import IconManager, Resources, load_ui_type
from blink.sessions import SessionManager, StreamDescription
from blink.util import QSingleton, run_in_gui_thread, translate


__all__ = ['ServerToolsWindow']


class WebPage(QWebEnginePage):
    def __init__(self, parent=None):
        super(WebPage, self).__init__(parent)
        disable_actions = {QWebEnginePage.WebAction.OpenLinkInNewBackgroundTab, QWebEnginePage.WebAction.OpenLinkInNewWindow, QWebEnginePage.WebAction.OpenLinkInThisWindow,
                           QWebEnginePage.WebAction.OpenLinkInNewTab,
                           QWebEnginePage.WebAction.DownloadLinkToDisk, QWebEnginePage.WebAction.DownloadImageToDisk,
                           QWebEnginePage.WebAction.DownloadMediaToDisk}
        for action in (self.action(action) for action in disable_actions):
            action.setVisible(False)
        self.call_link_clicked = False

    def createWindow(self, type):
        return self

    def acceptNavigationRequest(self, url, navigation_type, frame):
        if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in ('sip', 'sips'):
            blink = QApplication.instance()
            contact, contact_uri = URIUtils.find_contact(url.toString())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])
            blink.main_window.raise_()
            blink.main_window.activateWindow()
            self.call_link_clicked = True
            return False
        self.call_link_clicked = False
        return super(WebPage, self).acceptNavigationRequest(url, navigation_type, frame)


@implementer(IObserver)
class ServerToolsWebView(QWebEngineView):

    def __init__(self, parent=None):
        super(ServerToolsWebView, self).__init__(parent)
        self.setPage(WebPage(self))
        self.access_manager = Null
        self.authenticated = False
        self.account = None
        self.user_agent = 'blink'
        self.tab = None
        self.task = None
        self.last_error = None
        self.realm = None
        self.homepage = None
        self.urlChanged.connect(self._SH_URLChanged)

    @property
    def query_items(self):
        all_items = ('user_agent', 'tab', 'task', 'realm')
        return [(name, value) for name, value in self.__dict__.items() if name in all_items and value is not None]

    def _get_account(self):
        return self.__dict__['account']

    def _set_account(self, account):
        notification_center = NotificationCenter()
        old_account = self.__dict__.get('account', Null)
        if account is old_account:
            return
        self.__dict__['account'] = account
        self.authenticated = False
        if old_account:
            notification_center.remove_observer(self, sender=old_account)
        if account:
            notification_center.add_observer(self, sender=account)
            self.realm = account.id.domain
        else:
            self.realm = None
        try:
            self.page().authenticationRequired.disconnect(self._SH_AuthenticationRequired)
        except TypeError:
            pass
        self.page().authenticationRequired.connect(self._SH_AuthenticationRequired)

    account = property(_get_account, _set_account)
    del _get_account, _set_account

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if '__id__' in notification.data.modified or 'auth.password' in notification.data.modified:
            self.authenticated = False
            self.reload()

    def _SH_AuthenticationRequired(self, reply, auth):
        if self.account and not self.authenticated:
            auth.setUser(self.account.id.username)
            auth.setPassword(self.account.auth.password)
            self.authenticated = True
        else:
            # we were already authenticated, yet it asks for the auth again. this means our credentials are not good.
            # we do not provide credentials anymore in order to fail and not try indefinitely, but we also reset the
            # authenticated status so that we try again when the page is reloaded.
            self.last_error = 'Authentication failed'
            sip.assign(auth, QAuthenticator())
            self.authenticated = False

    def _SH_URLChanged(self, url):
        query_items = dict(QUrlQuery(url).queryItems())
        self.tab = query_items.get('tab') or self.tab
        self.task = query_items.get('task') or self.task

    def load_account_page(self, account, tab=None, task=None, reset_history=False, set_home=False):
        self.tab = tab
        self.task = task
        self.account = account
        url = QUrl(account.server.settings_url)
        url_query = QUrlQuery()
        for name, value in self.query_items:
            url_query.addQueryItem(name, value)
        url.setQuery(url_query)

        if set_home:
            self.homepage = url
        if reset_history:
            self.history().clear()
            self.page().runJavaScript('window.location.replace("{}");'.format(url.toString()))  # this will replace the current url in the history
        else:
            self.load(url)

    def load_homepage(self):
        self.load(self.homepage or self.history().itemAt(0).url())


ui_class, base_class = load_ui_type('server_tools.ui')


@implementer(IObserver)
class ServerToolsWindow(base_class, ui_class, metaclass=QSingleton):

    def __init__(self, model, parent=None):
        super(ServerToolsWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()
        self.setWindowTitle('Blink Server Tools')
        self.setWindowIcon(QIcon(Resources.get('icons/blink48.png')))
        self.model = model
        self.model.rowsInserted.connect(self._SH_ModelChanged)
        self.model.rowsRemoved.connect(self._SH_ModelChanged)
        self.account_button.menu().triggered.connect(self._SH_AccountButtonMenuTriggered)
        self.back_button.clicked.connect(self._SH_BackButtonClicked)
        self.back_button.triggered.connect(self._SH_NavigationButtonTriggered)
        self.forward_button.clicked.connect(self._SH_ForwardButtonClicked)
        self.forward_button.triggered.connect(self._SH_NavigationButtonTriggered)
        self.home_button.clicked.connect(self._SH_HomeButtonClicked)
        self.web_view.loadStarted.connect(self._SH_WebViewLoadStarted)
        self.web_view.loadFinished.connect(self._SH_WebViewLoadFinished)
        self.web_view.titleChanged.connect(self._SH_WebViewTitleChanged)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')

    def setupUi(self):
        super(ServerToolsWindow, self).setupUi(self)
        self.account_button.default_avatar = QIcon(Resources.get('icons/default-avatar.png'))
        self.account_button.setIcon(IconManager().get('avatar') or self.account_button.default_avatar)
        self.account_button.setMenu(QMenu(self.account_button))
        self.back_button.setMenu(QMenu(self.back_button))
        self.back_button.setEnabled(False)
        self.forward_button.setMenu(QMenu(self.forward_button))
        self.forward_button.setEnabled(False)

    def _SH_AccountButtonMenuTriggered(self, action):
        self.web_view.page().profile().clearHttpCache()
        self.web_view.page().triggerAction(QWebEnginePage.WebAction.Stop)
        account = action.data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab=self.web_view.tab, task=self.web_view.task, reset_history=account_changed, set_home=account_changed)

    def _SH_BackButtonClicked(self):
        self.web_view.history().back()

    def _SH_ForwardButtonClicked(self):
        self.web_view.history().forward()

    def _SH_NavigationButtonTriggered(self, action):
        self.web_view.history().goToItem(action.history_item)

    def _SH_HomeButtonClicked(self):
        self.web_view.load_homepage()

    def _SH_WebViewLoadStarted(self):
        self.spinner.show()

    def _SH_WebViewLoadFinished(self, load_ok):
        self.spinner.hide()
        if not load_ok and not self.web_view.page().call_link_clicked:
            icon_path = Resources.get('icons/invalid.png')
            error_message = self.web_view.last_error or 'Unknown error'
            html = """
            <html>
             <head>
              <style>
                .icon    { width: 64px; height: 64px; float: left; }
                .message { margin-left: 74px; line-height: 64px; vertical-align: middle; }
              </style>
             </head>
             <body>
              <img class="icon" src="file:%s" />
              <div class="message">Failed to load web page: <b>%s</b></div>
             </body>
            </html>
            """ % (icon_path, error_message)
            self.web_view.blockSignals(True)
            self.web_view.setHtml(html, baseUrl=QUrl.fromLocalFile(os.path.abspath(sys.argv[0])))
            self.web_view.blockSignals(False)
        self._update_navigation_buttons()

    def _SH_WebViewTitleChanged(self, title):
        self.window().setWindowTitle(translate('server_window', 'Blink Server Tools: {}').format(title))

    def _SH_ModelChanged(self, parent_index, start, end):
        menu = self.account_button.menu()
        menu.clear()
        for row in range(self.model.rowCount()):
            account_info = self.model.data(self.model.index(row, 0), Qt.ItemDataRole.UserRole)
            action = menu.addAction(account_info.name)
            action.setData(account_info.account)

    def open_settings_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='settings', reset_history=account_changed, set_home=True)
        self.show()

    def open_search_for_people_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='contacts', task='directory', reset_history=account_changed, set_home=True)
        self.show()

    def open_history_page(self, account):
        account = account or self.web_view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        account_changed = account is not self.web_view.account
        if account_changed:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
        self.account_button.setText(account.id)
        self.web_view.load_account_page(account, tab='calls', reset_history=account_changed, set_home=True)
        self.show()

    def _update_navigation_buttons(self):
        history = self.web_view.history()
        self.back_button.setEnabled(history.canGoBack())
        self.forward_button.setEnabled(history.canGoForward())
        back_menu = self.back_button.menu()
        back_menu.clear()
        for item in reversed(history.backItems(7)):
            action = back_menu.addAction(item.title())
            action.history_item = item
        forward_menu = self.forward_button.menu()
        forward_menu.clear()
        for item in history.forwardItems(7):
            action = forward_menu.addAction(item.title())
            action.history_item = item

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange', sender=BlinkSettings())

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'presence.icon' in notification.data.modified:
            self.account_button.setIcon(IconManager().get('avatar') or self.account_button.default_avatar)


del ui_class, base_class
