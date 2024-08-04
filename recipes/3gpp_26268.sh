#!/bin/sh
d=$(dirname $0)
dos2unix TS26.268_12.0.0-SourceCode/ecall/*
patch -p1 -u -i "$d/3gpp_26268.patch"