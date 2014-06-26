#!/usr/bin/env python

from distutils.core import setup
from distutils.extension import Extension
from Cython.Build import cythonize

from itertools import chain
import glob
import os
import re


def get_version():
    return re.search(r"""__version__\s+=\s+(?P<quote>['"])(?P<version>.+?)(?P=quote)""", open('blink/__init__.py').read()).group('version')

def find_packages(toplevel):
    return [directory.replace(os.path.sep, '.') for directory, subdirs, files in os.walk(toplevel) if '__init__.py' in files]

def list_resources(directory, destination_directory):
    return [(dir.replace(directory, destination_directory), [os.path.join(dir, file) for file in files]) for dir, subdirs, files in os.walk(directory)]

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
      ext_modules  = cythonize([Extension(name="blink.screensharing._rfb", sources=["blink/screensharing/_rfb.pyx"], libraries=["vncclient"])]),
      data_files   = list_resources('resources', destination_directory='share/blink'),
      scripts      = ['bin/blink']
)

