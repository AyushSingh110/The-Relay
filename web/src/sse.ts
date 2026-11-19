// A Server-Sent Events parser for a fetch() body. EventSource cannot POST, and /v1/ask
// is a POST, so the stream is read with fetch and parsed here (WHATWG SSE rules: events
// end at a blank line; "data" lines join with "\n"; lines starting with ":" are comments).

export interface RawEvent {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";
  private name = "";
  private data: string[] = [];

  /** Feed decoded text; returns the events completed by it. */
  feed(text: string): RawEvent[] {
    this.buffer += text;
    const events: RawEvent[] = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (match === null) break;
      // a lone "\r" at the very end may be the first half of "\r\n": wait for more
      if (match[0] === "\r" && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const event = this.line(line);
      if (event !== null) events.push(event);
    }
    return events;
  }

  private line(line: string): RawEvent | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.name = "";
        return null;
      }
      const event = { event: this.name || "message", data: this.data.join("\n") };
      this.name = "";
      this.data = [];
      return event;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.name = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}
