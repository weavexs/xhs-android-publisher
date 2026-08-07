#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

version="$(tr -d '[:space:]' < VERSION)"
tag="v${version}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区存在未提交修改，请先提交本次迭代。"
  exit 1
fi

if ! grep -Eq "^## (\\[)?${version}(\\])?( |$)" CHANGELOG.md; then
  echo "CHANGELOG.md 中缺少 ${version} 的发布说明。"
  exit 1
fi

git push origin main
git tag -a "$tag" -m "Release ${tag}"
git push origin "$tag"

notes="$(awk -v version="$version" '
  $0 ~ "^## (\\[)?" version "(\\])?( |$)" {capture=1; next}
  capture && /^## / {exit}
  capture {print}
' CHANGELOG.md)"

gh release create "$tag" --title "$tag" --notes "$notes"
echo "已发布 ${tag}"
