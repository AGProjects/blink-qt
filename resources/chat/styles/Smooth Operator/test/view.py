#!/usr/bin/python2

import os

from PyQt5.QtCore import QUrl
from PyQt5.QtWebKit import QWebSettings
from PyQt5.QtWebKitWidgets import QWebView
from PyQt5.QtWidgets import QApplication

app = QApplication(['mockup'])
view = QWebView()
settings = view.settings()
settings.setAttribute(QWebSettings.DeveloperExtrasEnabled, True)
view.load(QUrl.fromLocalFile(os.path.realpath('mockup.html')))
view.show()
app.exec_()
