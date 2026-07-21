#!/usr/bin/env bash
# Publish the locally built icechunk store to the Source Coop product prefix.
#
# Why sync instead of writing directly: icechunk commits need S3 server-side
# copy (CopyObject) for their atomic repo-info update, and the data.source.coop
# endpoint returns NotImplemented for it (verified 2026-07-21). Everything in an
# icechunk repo except the top-level "repo" file is immutable and content-
# addressed, so a plain file sync is safe as long as the mutable "repo" pointer
# is uploaded LAST - readers either see the old version or the complete new one.
set -euo pipefail

STORE=${1:?usage: publish.sh <local-store-dir> <account> <creds.json>}
ACCOUNT=${2:?usage: publish.sh <local-store-dir> <account> <creds.json>}
CREDS=${3:?usage: publish.sh <local-store-dir> <account> <creds.json>}

[ -f "$STORE/repo" ] || { echo "error: $STORE does not look like an icechunk store (no 'repo' file)"; exit 1; }

PRODUCT=$(uv run python -c "from usda_cdl import store; print(store.PRODUCT_NAME)")
SUBPATH=$(uv run python -c "from usda_cdl import store; print(store.STORE_SUBPATH)")
ENDPOINT=$(uv run python -c "from usda_cdl import store; print(store.SOURCE_COOP_ENDPOINT)")
REGION=$(uv run python -c "from usda_cdl import store; print(store.SOURCE_COOP_REGION)")
DEST="s3://${ACCOUNT}/${PRODUCT}/${SUBPATH}"

AWS_ACCESS_KEY_ID=$(python3 -c "import json; print(json.load(open('$CREDS'))['aws_access_key_id'])")
AWS_SECRET_ACCESS_KEY=$(python3 -c "import json; print(json.load(open('$CREDS'))['aws_secret_access_key'])")
AWS_SESSION_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['aws_session_token'])")
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# data.source.coop does not support multipart uploads (UploadPart fails), so
# force single-request PUTs. Our largest objects (zarr shards) are < 64 MiB,
# comfortably under the 5 GB single-PUT limit.
AWS_CONFIG_FILE=$(mktemp)
trap 'rm -f "$AWS_CONFIG_FILE"' EXIT
printf '[default]\ns3 =\n    multipart_threshold = 4GB\n' > "$AWS_CONFIG_FILE"
export AWS_CONFIG_FILE

echo "syncing $STORE -> $DEST (immutable files)"
aws s3 sync "$STORE" "$DEST" --exclude "repo" \
    --endpoint-url "$ENDPOINT" --region "$REGION" --only-show-errors

echo "uploading mutable 'repo' pointer last"
aws s3 cp "$STORE/repo" "$DEST/repo" \
    --endpoint-url "$ENDPOINT" --region "$REGION" --only-show-errors

echo "published $DEST"
