#!/bin/bash

# This script will build python3-sipsimple on MacOS
# https://github.com/AGProjects/python3-sipsimple
# https://raw.githubusercontent.com/AGProjects/python3-sipsimple/master/install_osx_user.sh

# Install Xcode from Apple
# Install MacPorts from https://www.macports.org
# Install python 3.9 from https://www.python.org/ftp/python/3.9.12/python-3.9.12-macosx10.9.pkg
# Install Mac Ports from https://www.macports.org

# Then run this script to install the packages in ~/Library/Python/3.9/lib/python/site-packages/

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

# Install C building dependencies
sudo port install yasm x264 gnutls openssl sqlite3 gnutls ffmpeg mpfr libmpc libvpx

# Install Python building dependencies

pip3 install --user cython==0.29.37 dnspython lxml twisted python-dateutil greenlet zope.interface requests gmpy2 wheel gevent

# Create a work directory

if [ ! -d work ]; then
    mkdir work
fi

cd work

# Download and build SIP SIMPLE client SDK built-in dependencies
for p in python3-application python3-eventlib python3-gnutls python3-otr python3-msrplib python3-xcaplib; do
    if [ ! -d $p ]; then
       darcs clone http://devel.ag-projects.com/repositories/$p
    fi
    cd $p
    pip3 install --user .
    cd ..
done

# Download and build SIP SIMPLE client SDK
if [ ! -d python3-sipsimple ]; then
    darcs clone http://devel.ag-projects.com/repositories/python3-sipsimple
fi

cd python3-sipsimple
./get_dependencies.sh 
pip3 install --user .
cd ..
