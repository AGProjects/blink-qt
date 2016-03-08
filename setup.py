#!/usr/bin/env python

import os

from distutils.core import setup
from distutils.extension import Extension
from Cython.Build import cythonize


class PackageInfo(object):
    def __init__(self, info_file):
        with open(info_file) as f:
            exec(f.read(), self.__dict__)
        self.__dict__.pop('__builtins__', None)

    def __getattribute__(self, name):  # this is here to silence the IDE about missing attributes
        return super(PackageInfo, self).__getattribute__(name)


def find_packages(root):
    return [directory.replace(os.path.sep, '.') for directory, sub_dirs, files in os.walk(root) if '__init__.py' in files]


def list_resources(source_directory, destination_directory):
    return [(directory.replace(source_directory, destination_directory), [os.path.join(directory, file) for file in files]) for directory, sub_dirs, files in os.walk(source_directory)]


package_info = PackageInfo(os.path.join('blink', '__info__.py'))
package_info.__description__ = "A state of the art, easy to use SIP client"


setup(
    name=package_info.__project__,
    version=package_info.__version__,

    description=package_info.__summary__,
    long_description=package_info.__description__,
    license=package_info.__license__,
    url=package_info.__webpage__,

    author=package_info.__author__,
    author_email=package_info.__email__,

    platforms=["Platform Independent"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: End Users/Desktop",
        "License :: GNU General Public License 3 (GPLv3)",
        "Operating System :: OS Independent",
        "Programming Language :: Python"
    ],

    packages=find_packages('blink'),
    ext_modules=cythonize([Extension(name="blink.screensharing._rfb", sources=["blink/screensharing/_rfb.pyx"], libraries=["vncclient"])]),
    data_files=list_resources('resources', destination_directory='share/blink'),
    scripts=['bin/blink']
)
