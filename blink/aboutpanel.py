# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['AboutPanel']

from PyQt4 import uic
from PyQt4.QtGui  import QFontMetrics

from blink import __date__, __version__
from blink.resources import Resources
from blink.util import QSingleton


credits_text = """
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">
<html>
<head>
<meta name="qrichtext" content="1" />
<style type="text/css">
 td.name { text-align: right; padding-right: 6px; }
 a:link  { text-decoration: none; color: #1f487f; }
</style>
</head>
<body>
<table width="100%" cellspacing="2" cellpadding="0" border="0">
 <tr><td class="name" align="right">AG Projects</td><td align="left"><a href="http://ag-projects.com/">http://ag-projects.com/</a></td></tr>
 <tr><td class="name" align="right">NLnet Foundation</td><td align="left"><a href="http://nlnet.nl/">http://nlnet.nl/</a></td></tr>
 <tr><td class="name" align="right">IETF Community</td><td align="left"><a href="http://ietf.org/">http://ietf.org/</a></td></tr>
 <tr><td class="name" align="right">SIP Simple Client</td><td align="left"><a href="http://sipsimpleclient.com/">http://sipsimpleclient.com/</a></td></tr>
</table>
</body>
</html>
"""


ui_class, base_class = uic.loadUiType(Resources.get('about_panel.ui'))

class AboutPanel(base_class, ui_class):
    __metaclass__ = QSingleton

    def __init__(self, parent=None):
        super(AboutPanel, self).__init__(parent)

        with Resources.directory:
            self.setupUi(self)

        self.version.setText(u'Version %s\n%s' % (__version__, __date__))

        credits_width = QFontMetrics(self.credits_text.currentFont()).width("NLNET Foundation" + "http://sipsimpleclient.com") + 40
        self.credits_text.setFixedWidth(credits_width)
        self.credits_text.document().documentLayout().documentSizeChanged.connect(self._credits_size_changed)
        self.credits_text.setHtml(credits_text)

    def _credits_size_changed(self, size):
        self.credits_text.document().documentLayout().documentSizeChanged.disconnect(self._credits_size_changed)
        self.setFixedSize(self.minimumSize().width(), self.minimumSize().width()*1.40) # set a fixed aspect ratio
        row_height = QFontMetrics(self.credits_text.currentFont()).height() + 2 # +2 for cellspacing
        max_credits_height = 8*row_height + 2 + 14 # allow for maximum 8 rows; +2 for cellspacing and +14 for top/bottom margins
        if self.credits_text.height() > max_credits_height:
            self.setFixedHeight(self.height() - (self.credits_text.height() - max_credits_height))

del ui_class, base_class

