#!/bin/bash
# Install Xcode from Apple
# Install MacPorts from https://www.macports.org
# Install python 3.9 from https://www.python.org/ftp/python/3.9.12/python-3.9.12-macosx10.9.pkg
# Install Mac Ports from https://www.macports.org

if [ ! -d /Applications/Python\ 3.9/ ]; then
    echo
    echo "Please install Python 3.9 from https://www.python.org/ftp/python/3.9.12/python-3.9.12-macosx10.9.pkg"
    echo
    exit 1
fi

which port > /dev/null
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Please install Mac Ports from https://www.macports.org"
    echo
    exit 1
fi

which darcs > /dev/null
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo "AG Projects repositories are managed using darcs"
    echo
    echo "Install darcs from http://darcs.net: wget http://darcs.net/binaries/macosx/darcs-2.14.1.tar.gz"
    echo "or using Mac Ports: sudo port install darcs"
    echo
    exit 1
fi

chmod +x install_sipsimple.sh
./install_sipsimple.sh

RESULT=$?
if [ $RESULT -ne 0 ]; then
    exit 1
fi

cd ..
pip3 install --user -r macos/requirements-osx.txt
sudo port install libvncserver upx
export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
./build_inplace
pip3 install --user .
