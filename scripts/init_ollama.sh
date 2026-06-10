#!/usr/bin/env bash
# Pull models required by Omni into the local Ollama instance.
set -euo pipefail

echo "Pulling qwen2.5-coder:7b ..."
ollama pull qwen2.5-coder:7b

echo "Pulling nomic-embed-text ..."
ollama pull nomic-embed-text

echo "Done. Verify:"
ollama list
