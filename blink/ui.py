# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['main_window']


import os
from PyQt4 import uic
from blink.resources import Resources

original_directory = os.getcwd()
os.chdir(Resources.directory)

main_window = uic.loadUi(Resources.get('blink.ui'))

os.chdir(original_directory)
del original_directory
