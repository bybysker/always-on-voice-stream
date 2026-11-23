#!/usr/bin/env bash
# Build the Windows app and its zip on a Windows x64 PC (what .github/workflows/windows-app.yml runs).
# Runs in Git Bash, with Visual Studio's C++ tools (or Build Tools), CMake, git and a Python 3.12:
#   pip install -e '.[audio,dashboard,agents,app,windows,dev]' pyinstaller==6.22.3
#
#   packaging/windows/build.sh [step ...]     default: whisper check-whisper icon app selftest zip
#
# Steps:
#   whisper        build whisper-server.exe from whisper.cpp $WHISPER_CPP_VERSION (static, static C runtime, AVX2)
#   check-whisper  fail unless whisper-server.exe needs only Windows' own DLLs, and runs
#   icon           draw the icon (packaging/windows/build/Voicestream.ico)
#   app            PyInstaller -> dist/Voicestream/Voicestream.exe (whisper-server.exe in _internal/bin)
#   selftest       start the packaged app with --selftest: its icon next to the clock, no browser, no listening;
#                  quit through the icon's menu, check the JSON report
#   zip            dist/Voicestream-<version>-windows-x64.zip (the Voicestream folder)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"
WHISPER_CPP_VERSION="${WHISPER_CPP_VERSION:-v1.9.5}"
BUILD="$ROOT/build"
WHISPER_SRC="$BUILD/whisper.cpp-$WHISPER_CPP_VERSION"
WHISPER_BUILD="$BUILD/whisper-build-win"
export VOICESTREAM_WHISPER_SERVER="${VOICESTREAM_WHISPER_SERVER:-$BUILD/whisper/bin/whisper-server.exe}"
APP="$ROOT/dist/Voicestream"
SELFTEST_DIR="${SELFTEST_DIR:-$BUILD/selftest-windows}"
SELFTEST_SECONDS="${SELFTEST_SECONDS:-20}"

say_step() { printf '\n==> %s\n' "$*"; }
fail() { echo "error: $*" >&2; exit 1; }

step_whisper() {
  say_step "whisper-server $WHISPER_CPP_VERSION"
  if [[ ! -d "$WHISPER_SRC/.git" ]]; then
    rm -rf "$WHISPER_SRC"
    git clone --depth 1 --branch "$WHISPER_CPP_VERSION" https://github.com/ggml-org/whisper.cpp "$WHISPER_SRC"
  fi
  # static (one .exe), the static C runtime (no Visual C++ Redistributable needed), no OpenMP DLL,
  # portable code for x64 CPUs with AVX2 (Intel since 2013, AMD since 2015)
  cmake -S "$WHISPER_SRC" -B "$WHISPER_BUILD" -A x64 \
    -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
    -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
    -DCMAKE_POLICY_DEFAULT_CMP0091=NEW -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded \
    -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF \
    -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_TESTS=OFF
  cmake --build "$WHISPER_BUILD" --config Release --target whisper-server -j "${NUMBER_OF_PROCESSORS:-4}"
  local built
  built="$(find "$WHISPER_BUILD" -name whisper-server.exe -path '*Release*' | head -n 1)"
  [[ -n "$built" ]] || fail "the build made no whisper-server.exe"
  mkdir -p "$(dirname "$VOICESTREAM_WHISPER_SERVER")"
  cp "$built" "$VOICESTREAM_WHISPER_SERVER"
  echo "$VOICESTREAM_WHISPER_SERVER"
}

step_check_whisper() {
  say_step "whisper-server.exe needs only Windows' own DLLs"
  [[ -f "$VOICESTREAM_WHISPER_SERVER" ]] || fail "no whisper-server at $VOICESTREAM_WHISPER_SERVER"
  "$PYTHON" packaging/windows/check_exe.py "$VOICESTREAM_WHISPER_SERVER"
  "$VOICESTREAM_WHISPER_SERVER" --help >/dev/null 2>&1 || fail "whisper-server --help failed"
}

step_icon() {
  say_step "icon"
  "$PYTHON" packaging/windows/make_icon.py
}

step_app() {
  say_step "PyInstaller"
  rm -rf "$APP"
  "$PYTHON" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller-win" \
    packaging/windows/voicestream.spec
  [[ -f "$APP/Voicestream.exe" ]] || fail "PyInstaller made no $APP/Voicestream.exe"
  [[ -f "$APP/_internal/bin/whisper-server.exe" ]] || fail "whisper-server.exe is not in the app"
  du -sh "$APP"
}

step_selftest() {
  say_step "self-test of the packaged app (${SELFTEST_SECONDS} s)"
  rm -rf "$SELFTEST_DIR"; mkdir -p "$SELFTEST_DIR"
  local status=0
  # without the build tree's whisper-server: the app must find its own
  # a windowed app: no console output, everything is in the report (and the app's log under ~/.voicestream)
  env -u VOICESTREAM_WHISPER_SERVER "$APP/Voicestream.exe" \
    --selftest "$SELFTEST_SECONDS" --selftest-report "$SELFTEST_DIR/selftest.json" \
    >"$SELFTEST_DIR/selftest.log" 2>&1 || status=$?
  echo "exit status: $status"
  cat "$SELFTEST_DIR/selftest.log" || true
  "$PYTHON" packaging/windows/check_selftest.py "$SELFTEST_DIR/selftest.json"
  return "$status"
}

step_zip() {
  say_step "zip"
  local version zip
  version="$("$PYTHON" packaging/mac/bundle_info.py version)"
  zip="$ROOT/dist/Voicestream-$version-windows-x64.zip"
  rm -f "$zip"
  "$PYTHON" -c 'import shutil, sys; shutil.make_archive(sys.argv[1][:-4], "zip", sys.argv[2], "Voicestream")' \
    "$zip" "$ROOT/dist"
  du -h "$zip"
  echo "$zip"
}

steps=("$@")
(( ${#steps[@]} )) || steps=(whisper check-whisper icon app selftest zip)
for step in "${steps[@]}"; do
  case "$step" in
    whisper) step_whisper ;;
    check-whisper) step_check_whisper ;;
    icon) step_icon ;;
    app) step_app ;;
    selftest) step_selftest ;;
    zip) step_zip ;;
    *) fail "unknown step: $step (whisper check-whisper icon app selftest zip)" ;;
  esac
done
