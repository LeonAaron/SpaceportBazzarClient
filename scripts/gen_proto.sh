#!/usr/bin/env bash
# Regenerate the Python protobuf bindings from the supplied schema.
# Run inside the container: docker compose exec bazaar scripts/gen_proto.sh
set -euo pipefail

cd "$(dirname "$0")/.."

protoc -I bazaar-protobuf-starter-linux \
  --python_out=. \
  bazaar-protobuf-starter-linux/bazaar.proto

python -c 'import bazaar_pb2; print("bazaar_pb2 generated OK")'
