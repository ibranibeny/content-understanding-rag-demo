import type { ChatEvent } from "../domain/types";

function decodeBlock(block: string): ChatEvent | null {
  let type = "";
  const data: string[] = [];
  for (const raw of block.split(/\r?\n/)) {
    if (raw.startsWith("event:")) type = raw.slice(6).trim();
    if (raw.startsWith("data:")) data.push(raw.slice(5).trimStart());
  }
  if (!type || !["retrieval", "token", "citation", "done", "error"].includes(type)) return null;
  try { return { type, ...JSON.parse(data.join("\n")) } as ChatEvent; } catch { return null; }
}

export async function* parseSseStream(stream: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const abort = () => void reader.cancel();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    while (!signal?.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.search(/\r?\n\r?\n/);
      while (boundary >= 0) {
        const separator = buffer.slice(boundary).match(/^\r?\n\r?\n/)?.[0] ?? "\n\n";
        const event = decodeBlock(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + separator.length);
        if (event) yield event;
        boundary = buffer.search(/\r?\n\r?\n/);
      }
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    if (signal?.aborted) await reader.cancel();
    else reader.releaseLock();
  }
}

export async function streamChat(body: object, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
  const response = await fetch("/api/chat/stream", {
    method: "POST", credentials: "include", signal,
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) throw new Error("Chat is unavailable.");
  return response.body;
}
