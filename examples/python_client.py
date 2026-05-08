"""
Example: Using PrivAiTe with the OpenAI Python SDK.

pip install openai
"""

from openai import OpenAI

client = OpenAI(
    api_key="sk-privaite-your-key",
    base_url="http://localhost:8400/v1",
)

models = client.models.list()
print("Available models:", [m.id for m in models.data])

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "user", "content": "My name is Sarah Johnson, my email is sarah@acme.com."},
    ],
)
print("Response:", response.choices[0].message.content)

print("\nStreaming:")
stream = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "user", "content": "My name is Sarah Johnson. Say hello using my name."},
    ],
    stream=True,
)
for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
print()
