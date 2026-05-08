#!/bin/bash
# Quick test of PrivAiTe proxy with curl

BASE_URL="http://localhost:8400"
API_KEY="sk-privaite-your-key"

echo "=== Health ==="
curl -s "$BASE_URL/health"
echo

echo "=== Models ==="
curl -s "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
echo

echo "=== Chat ==="
curl -s "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "My name is John Smith, email john@acme.com. Summarize my info."}
    ]
  }' | python3 -m json.tool
echo

echo "=== Streaming ==="
curl -sN "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "stream": true,
    "messages": [
      {"role": "user", "content": "My name is John Smith. Say hi."}
    ]
  }'
