#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT/public_data/proteingym"
mkdir -p "$OUT_DIR"

DATASET="dms_substitutions"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)
      DATASET="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$DATASET" in
  dms_substitutions)
    KEY="DMS_substitutions.parquet"
    ;;
  dms_indels)
    KEY="DMS_indels.parquet"
    ;;
  clinical_substitutions)
    KEY="clinical_substitutions.parquet"
    ;;
  clinical_indels)
    KEY="clinical_indels.parquet"
    ;;
  *)
    echo "unknown dataset: $DATASET" >&2
    exit 2
    ;;
esac

DEST="$OUT_DIR/$KEY"
if [[ -s "$DEST" ]]; then
  echo "already exists: $DEST"
  exit 0
fi

echo "downloading ProteinGym $KEY to $DEST"
if command -v aws >/dev/null 2>&1; then
  aws s3 cp --no-sign-request "s3://proteingym/$KEY" "$DEST"
else
  curl -L --fail --continue-at - \
    "https://proteingym.s3.us-east-2.amazonaws.com/$KEY" \
    -o "$DEST"
fi

echo "downloaded: $DEST"

