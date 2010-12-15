#!/usr/bin/env python

from distutils.core import setup
from itertools import chain
import glob
import os
import re


def get_version():
    return re.search(r"""__version__\s+=\s+(?P<quote>['"])(?P<version>.+?)(?P=quote)""", open('blink/__init__.py').read()).group('version')

def find_packages(toplevel):
    return [directory.replace(os.path.sep, '.') for directory, subdirs, files in os.walk(toplevel) if '__init__.py' in files]

setup(name         = "blink",
      version      = get_version(),
      author       = "AG Projects",
      author_email = "support@ag-projects.com",
      url          = "http://icanblink.com",
      description  = "Blink Qt",
      long_description = "A state of the art, easy to use SIP client",
      platforms    = ["Platform Independent"],
      classifiers  = [
          "Development Status :: 4 - Beta",
          "Intended Audience :: End Users/Desktop",
          "License :: GNU General Public License 3 (GPLv3)",
          "Operating System :: OS Independent",
          "Programming Language :: Python"
      ],
      packages     = find_packages('blink'),
      data_files   = [('share/blink', glob.glob('resources/*.ui')),
                      ('share/blink/icons', list(chain(*(glob.glob('resources/icons/*.%s' % ext) for ext in ('png', 'svg', 'mng', 'ico'))))),
                      ('share/blink/sounds', glob.glob('resources/sounds/*.wav'))
      ],
      scripts      = ['bin/blink']
)

