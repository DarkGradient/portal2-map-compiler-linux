#!/bin/sh
set -eu
VM_OPTIONS=
BASEDIR=$(dirname "$0")
"$BASEDIR/bin/java" $VM_OPTIONS -m info.ata4.bspsrc.app/info.ata4.bspsrc.app.src.BspSourceLauncher $*