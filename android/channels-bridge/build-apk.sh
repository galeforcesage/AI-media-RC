#!/bin/bash
set -e
export JAVA_HOME=/home/sagetv/jdk-21.0.10+7
export PATH=$JAVA_HOME/bin:$PATH
export ANDROID_HOME=/home/sagetv/android-sdk
cd /home/sagetv/channels-bridge-build
echo "Java: $(java -version 2>&1 | head -1)"
echo "Building Release APK..."
./gradlew assembleRelease --no-daemon 2>&1
echo "---BUILD DONE---"
ls -lh app/build/outputs/apk/release/*.apk 2>/dev/null || echo "NO APK FOUND"
