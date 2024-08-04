#!/bin/bash

# This script will build python3-sipsimple on MacOS
# https://github.com/AGProjects/python3-sipsimple

# Install Xcode from Apple
# Install MacPorts from https://www.macports.org
# Install python 3.9 from https://www.python.org/ftp/python/3.9.12/python-3.9.12-macosx10.9.pkg
#
# Then run this script to install the packages in ~/Library/Python/3.9/lib/python/site-packages/

# Install C building dependencies
sudo port install darcs yasm x264 gnutls openssl sqlite3 gnutls ffmpeg mpfr libmpc libvpx

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

