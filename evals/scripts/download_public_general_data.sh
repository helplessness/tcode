#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASET="simpleqa"

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
  simpleqa)
    OUT_DIR="$ROOT/public_data/simpleqa"
    mkdir -p "$OUT_DIR"
    DEST="$OUT_DIR/simple_qa_test_set.csv"
    URL="https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv"
    ;;
  *)
    echo "unknown dataset: $DATASET" >&2
    exit 2
    ;;
esac

if [[ -s "$DEST" ]]; then
  echo "already exists: $DEST"
  exit 0
fi

echo "downloading $DATASET to $DEST"
curl -L --fail "$URL" -o "$DEST"
echo "downloaded: $DEST"

