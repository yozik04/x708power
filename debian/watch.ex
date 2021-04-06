# Compulsory line, this is a version 4 file
version=4

# GitHub hosted projects
opts="filenamemangle=s%(?:.*?)?v?(\d[\d.]*)\.tar\.gz%x708power-$1.tar.gz%" \
   https://github.com/yozik04/x708power/tags \
   (?:.*?/)?v?(\d[\d.]*)\.tar\.gz debian uupdate
