#!/usr/bin/env bash
# Pull models required by Omni into the local Ollama instance.
set -euo pipefail

echo "Pulling qwen3.6 ..."
ollama pull qwen3.6

echo "Pulling nomic-embed-text ..."
ollama pull nomic-embed-text

echo "Done. Verify:"
ollama list
