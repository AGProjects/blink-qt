"""
Usage from top directory:
    python3 macos/make-macos-app.py py2app
"""

from setuptools import setup

APP = ['blink-run.py']
DATA_FILES = ['blink', 'macos']
OPTIONS = {'iconfile': 'macos/blink.icns', 'plist': 'macos/Info.plist'}

setup(
    app=APP,
    name='Blink',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
