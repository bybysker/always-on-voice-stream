#!/usr/bin/env bash
# Attach one build to the release $TAG, and publish the release once it has both apps.
#
#   TAG=v0.4.0 GH_REPO=owner/repo GH_TOKEN=… packaging/publish-release.sh dist/Voicestream-0.4.0-arm64.dmg
#
# Run by the release jobs of mac-app.yml (the DMG) and windows-app.yml (the Windows zip). release.yml
# makes the release first, as a draft: nobody sees a draft, not even the apps' updater. Each build
# attaches its file to it; the one that finds both files there publishes it. So a release always comes
# out with the Mac and the Windows app, or stays a draft (a build failed: run it again, it attaches).
# A tag pushed by hand has no draft yet: the first build to finish makes it.
set -euo pipefail

FILE="${1:?the file to attach}"
: "${TAG:?}"
NOTES="${NOTES:-packaging/mac/release-notes.md}"

mac_asset() { grep -qE -- "-${TAG#v}-arm64\.dmg$"; }
windows_asset() { grep -qE -- "-${TAG#v}-windows-x64\.zip$"; }

if gh release view "$TAG" >/dev/null 2>&1; then
  gh release upload "$TAG" "$FILE" --clobber  # into the draft, or a re-run replacing the file
else
  verify=(); [[ "${GITHUB_REF:-}" == refs/tags/* ]] && verify=(--verify-tag)
  # a tag with a suffix (v0.4.0-beta.1) is a pre-release: only apps that asked for those see it
  pre=(); [[ "$TAG" == *-* ]] && pre=(--prerelease)
  # ${a[@]+"${a[@]}"}: an empty array under set -u, also in macOS's bash 3.2
  gh release create "$TAG" "$FILE" --draft ${verify[@]+"${verify[@]}"} ${pre[@]+"${pre[@]}"} --target "$GITHUB_SHA" \
    --title "Voicestream $TAG" --notes-file "$NOTES" --generate-notes
fi

assets="$(gh release view "$TAG" --json assets --jq '.assets[].name')"
echo "$assets"
draft="$(gh release view "$TAG" --json isDraft --jq .isDraft)"
if [[ "$draft" != true ]]; then
  echo "the release $TAG is already published"
elif mac_asset <<<"$assets" && windows_asset <<<"$assets"; then
  gh release edit "$TAG" --draft=false
  echo "published $TAG: the Mac and the Windows app are both attached"
else
  echo "the release $TAG stays a draft until the other app's build attaches its file"
fi
