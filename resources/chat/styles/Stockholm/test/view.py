#!/usr/bin/python

from PyQt5.QtCore import QUrl
from PyQt5.QtWebKit import QWebSettings
from PyQt5.QtWebKitWidgets import QWebView
from PyQt5.QtWidgets import QApplication

app = QApplication([])
view = QWebView()
settings = view.settings()
settings.setAttribute(QWebSettings.DeveloperExtrasEnabled, True)
view.load(QUrl('mockup.html'))
view.show()
app.exec_()
