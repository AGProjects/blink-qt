#!/usr/bin/python

from PyQt4.QtCore import QUrl
from PyQt4.QtGui import QApplication
from PyQt4.QtWebKit import QWebView, QWebSettings

app = QApplication([])
view = QWebView()
settings = view.settings()
settings.setAttribute(QWebSettings.DeveloperExtrasEnabled, True)
view.load(QUrl('mockup.html'))
view.show()
app.exec_()
