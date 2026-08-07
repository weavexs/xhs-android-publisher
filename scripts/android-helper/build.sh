#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SDK=${ANDROID_HOME:-"$HOME/Library/Android/sdk"}
JAVA_HOME=${JAVA_HOME:-"/Applications/Android Studio.app/Contents/jbr/Contents/Home"}
export JAVA_HOME
BUILD_TOOLS="$SDK/build-tools/35.0.0"
ANDROID_JAR="$SDK/platforms/android-35/android.jar"
BUILD="$ROOT/build"
CLASSES="$BUILD/classes"
DEX="$BUILD/dex"

rm -rf "$BUILD"
mkdir -p "$CLASSES" "$DEX"

"$JAVA_HOME/bin/javac" \
  -source 8 \
  -target 8 \
  -bootclasspath "$ANDROID_JAR" \
  -d "$CLASSES" \
  "$ROOT/src/com/codex/xhsinput/ClipboardReceiver.java"

"$BUILD_TOOLS/aapt2" link \
  --manifest "$ROOT/AndroidManifest.xml" \
  -I "$ANDROID_JAR" \
  -o "$BUILD/helper-unsigned.apk"

"$BUILD_TOOLS/d8" \
  --lib "$ANDROID_JAR" \
  --output "$DEX" \
  "$CLASSES/com/codex/xhsinput/ClipboardReceiver.class"

(
  cd "$DEX"
  zip -q -j "$BUILD/helper-unsigned.apk" classes.dex
)

"$BUILD_TOOLS/zipalign" -f 4 \
  "$BUILD/helper-unsigned.apk" \
  "$BUILD/helper-aligned.apk"

"$BUILD_TOOLS/apksigner" sign \
  --ks "$HOME/.android/debug.keystore" \
  --ks-key-alias androiddebugkey \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$BUILD/xhs-input-helper.apk" \
  "$BUILD/helper-aligned.apk"

"$BUILD_TOOLS/apksigner" verify "$BUILD/xhs-input-helper.apk"
echo "$BUILD/xhs-input-helper.apk"
