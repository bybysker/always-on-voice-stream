#!/usr/bin/env bash
# Build Voicestream.app and its DMG on an Apple Silicon Mac (what .github/workflows/mac-app.yml runs).
#
#   packaging/mac/build.sh [step ...]        default: whisper check-whisper icon app verify dmg
#
# Steps:
#   whisper        build whisper-server from whisper.cpp $WHISPER_CPP_VERSION (static, Metal embedded)
#   check-whisper  fail unless whisper-server links only macOS's own libraries (/usr/lib, /System)
#   icon           draw the app icon (packaging/mac/build/Voicestream.icns)
#   app            PyInstaller -> dist/Voicestream.app (whisper-server bundled through the spec)
#   verify         codesign --verify --deep --strict, Info.plist, architecture, bundled whisper-server runs
#   selftest       launch the app with --selftest, take screenshots, check the JSON report
#   dmg            dist/Voicestream-<version>-arm64.dmg (the app + an Applications shortcut)
#
# Needs: Xcode command line tools, cmake, git, and a Python 3.12 with
#   pip install -e '.[audio,dashboard,agents,app,mac]' pyinstaller==6.22.3
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
WHISPER_CPP_VERSION="${WHISPER_CPP_VERSION:-v1.9.5}"
BUILD="$ROOT/build"
WHISPER_SRC="$BUILD/whisper.cpp-$WHISPER_CPP_VERSION"
WHISPER_BUILD="$BUILD/whisper-build"
export VOICESTREAM_WHISPER_SERVER="${VOICESTREAM_WHISPER_SERVER:-$BUILD/whisper/bin/whisper-server}"
export VOICESTREAM_ICON="${VOICESTREAM_ICON:-$ROOT/packaging/mac/build/Voicestream.icns}"
APP="$ROOT/dist/Voicestream.app"
SELFTEST_DIR="${SELFTEST_DIR:-$BUILD/selftest}"
SELFTEST_SECONDS="${SELFTEST_SECONDS:-30}"

say_step() { printf '\n==> %s\n' "$*"; }
fail() { echo "error: $*" >&2; exit 1; }

step_whisper() {
  say_step "whisper-server $WHISPER_CPP_VERSION"
  if [[ ! -d "$WHISPER_SRC/.git" ]]; then
    rm -rf "$WHISPER_SRC"
    git clone --depth 1 --branch "$WHISPER_CPP_VERSION" https://github.com/ggml-org/whisper.cpp "$WHISPER_SRC"
  fi
  # static (one self-contained binary), portable CPU code, Metal shaders embedded, Accelerate BLAS
  cmake -S "$WHISPER_SRC" -B "$WHISPER_BUILD" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
    -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DGGML_BLAS=ON \
    -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0 \
    -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF \
    -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_TESTS=OFF
  cmake --build "$WHISPER_BUILD" --config Release --target whisper-server -j "$(sysctl -n hw.ncpu)"
  mkdir -p "$(dirname "$VOICESTREAM_WHISPER_SERVER")"
  cp "$WHISPER_BUILD/bin/whisper-server" "$VOICESTREAM_WHISPER_SERVER"
  echo "$VOICESTREAM_WHISPER_SERVER"
}

step_check_whisper() {
  say_step "whisper-server links only system libraries"
  [[ -x "$VOICESTREAM_WHISPER_SERVER" ]] || fail "no whisper-server at $VOICESTREAM_WHISPER_SERVER"
  otool -L "$VOICESTREAM_WHISPER_SERVER"
  local foreign
  foreign="$(otool -L "$VOICESTREAM_WHISPER_SERVER" | tail -n +2 | awk '{print $1}' | grep -v -E '^(/usr/lib/|/System/Library/)' || true)"
  [[ -z "$foreign" ]] || fail "whisper-server needs libraries a Mac doesn't have: $foreign"
  lipo -archs "$VOICESTREAM_WHISPER_SERVER" | grep -qw arm64 || fail "whisper-server is not arm64"
  "$VOICESTREAM_WHISPER_SERVER" --help >/dev/null 2>&1 || fail "whisper-server --help failed"
}

step_icon() {
  say_step "app icon"
  "$PYTHON" packaging/mac/make_icon.py --out "$(dirname "$VOICESTREAM_ICON")"
}

step_app() {
  say_step "PyInstaller"
  rm -rf "$APP" "$ROOT/dist/Voicestream"
  "$PYTHON" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller" \
    packaging/mac/voicestream.spec
  [[ -d "$APP" ]] || fail "PyInstaller made no $APP"
  du -sh "$APP"
}

bundled_whisper() {
  local p
  for p in "$APP/Contents/Frameworks/bin/whisper-server" "$APP/Contents/Resources/bin/whisper-server"; do
    [[ -x "$p" ]] && { echo "$p"; return 0; }
  done
  return 1
}

step_verify() {
  say_step "verify the bundle"
  codesign --verify --deep --strict --verbose=2 "$APP"
  codesign -dv --verbose=2 "$APP" 2>&1 | grep -E '^(Identifier|Format|Signature|TeamIdentifier|CodeDirectory)' || true
  plutil -lint "$APP/Contents/Info.plist"
  plutil -p "$APP/Contents/Info.plist"
  [[ "$(plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist")" == "io.github.bybysker.voicestream" ]] \
    || fail "wrong bundle identifier"
  lipo -archs "$APP/Contents/MacOS/Voicestream" | grep -qw arm64 || fail "the app is not arm64"
  local ws
  ws="$(bundled_whisper)" || fail "whisper-server is not in the bundle"
  codesign --verify --strict "$ws"
  "$ws" --help >/dev/null 2>&1 || fail "the bundled whisper-server does not run"
  echo "bundled whisper-server: ${ws#"$APP/"}"
}

wait_phase() {  # wait_phase <phase> <seconds>: the self-test writes how far it got to <report>.phase
  local phase="$1" limit="$2" file="$SELFTEST_DIR/selftest.json.phase" waited=0
  while (( waited < limit * 2 )); do
    [[ -f "$file" ]] && grep -qx "$phase" "$file" && return 0
    [[ -f "$file" ]] && grep -qx -E 'done|hung' "$file" && return 1
    sleep 0.5; waited=$((waited + 1))
  done
  return 1
}

step_selftest() {
  say_step "self-test launch (${SELFTEST_SECONDS} s)"
  rm -rf "$SELFTEST_DIR"; mkdir -p "$SELFTEST_DIR"
  # run the binary itself, not `open`: the job's own permissions apply and its output stays here
  "$APP/Contents/MacOS/Voicestream" --selftest "$SELFTEST_SECONDS" --selftest-report "$SELFTEST_DIR/selftest.json" \
    >"$SELFTEST_DIR/selftest.log" 2>&1 &
  local pid=$! status=0
  if wait_phase window 90; then screencapture -x "$SELFTEST_DIR/window.png" || echo "screenshot failed"; fi
  if wait_phase menu 30; then screencapture -x "$SELFTEST_DIR/menu-bar.png" || echo "screenshot failed"; fi
  local deadline=$((SECONDS + SELFTEST_SECONDS + 120))
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS > deadline )); then echo "the app did not quit: killing it"; kill -9 "$pid" || true; break; fi
    sleep 1
  done
  wait "$pid" || status=$?
  echo "exit status: $status"
  cat "$SELFTEST_DIR/selftest.log" || true
  "$PYTHON" - "$SELFTEST_DIR/selftest.json" <<'EOF'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(report, indent=2))
failed = [name for name, ok in report.get("checks", {}).items() if not ok]
print("failed checks:", failed or "none", "| errors:", report.get("errors") or "none")
sys.exit(0 if report.get("ok") else 1)
EOF
  return "$status"
}

step_dmg() {
  say_step "DMG"
  local version stage dmg
  version="$("$PYTHON" packaging/mac/bundle_info.py version)"
  stage="$BUILD/dmg"
  dmg="$ROOT/dist/Voicestream-$version-arm64.dmg"
  rm -rf "$stage" "$dmg"; mkdir -p "$stage"
  ditto "$APP" "$stage/Voicestream.app"  # keeps symlinks, permissions and the signature intact
  ln -s /Applications "$stage/Applications"
  local attempt
  for attempt in 1 2 3; do  # hdiutil on CI runners sometimes fails with "Resource busy"
    hdiutil create -volname Voicestream -srcfolder "$stage" -ov -format UDZO "$dmg" && break
    [[ $attempt == 3 ]] && fail "hdiutil create failed"
    sleep 5
  done
  hdiutil verify "$dmg"
  du -h "$dmg"
  echo "$dmg"
}

steps=("$@")
(( ${#steps[@]} )) || steps=(whisper check-whisper icon app verify dmg)
for step in "${steps[@]}"; do
  case "$step" in
    whisper) step_whisper ;;
    check-whisper) step_check_whisper ;;
    icon) step_icon ;;
    app) step_app ;;
    verify) step_verify ;;
    selftest) step_selftest ;;
    dmg) step_dmg ;;
    *) fail "unknown step: $step (whisper check-whisper icon app verify selftest dmg)" ;;
  esac
done
