/**
 * Example: Using PrivAiTe with the OpenAI Node.js SDK.
 *
 * npm install openai
 */

import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "sk-privaite-your-key",
  baseURL: "http://localhost:8400/v1",
});

const models = await client.models.list();
console.log(
  "Models:",
  models.data.map((m) => m.id)
);

const response = await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [
    {
      role: "user",
      content: "My name is Sarah Johnson, my email is sarah@acme.com.",
    },
  ],
});
console.log("Response:", response.choices[0].message.content);

console.log("\nStreaming:");
const stream = await client.chat.completions.create({
  model: "gpt-4o-mini",
  stream: true,
  messages: [
    {
      role: "user",
      content: "My name is Sarah Johnson. Say hello using my name.",
    },
  ],
});
for await (const chunk of stream) {
  const content = chunk.choices[0]?.delta?.content;
  if (content) process.stdout.write(content);
}
console.log();
