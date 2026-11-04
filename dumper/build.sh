#!/bin/sh
# Rebuild src/android_control/assets/ac-dump.jar (needs a JDK and Android SDK build-tools + a platform).
set -eu
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}}"
PLATFORM="$(ls -d "$SDK"/platforms/android-* | sort -V | tail -1)"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
rm -rf build && mkdir -p build/classes
javac -source 8 -target 8 -nowarn -Xlint:-options -bootclasspath "$PLATFORM/android.jar" -d build/classes AcDump.java
"$BT/d8" --min-api 21 --lib "$PLATFORM/android.jar" --output build build/classes/AcDump.class
mkdir -p ../src/android_control/assets
(cd build && rm -f ../../src/android_control/assets/ac-dump.jar && zip -q ../../src/android_control/assets/ac-dump.jar classes.dex)
echo "built src/android_control/assets/ac-dump.jar"
