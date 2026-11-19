import { SSEParser } from "../sse";

test("events are split at blank lines, in any chunking", () => {
  const wire =
    'event: accepted\ndata: {"a":1}\n\n: comment\nevent: sentence\r\ndata: x\r\ndata: y\r\n\r\n';
  for (let size = 1; size <= wire.length; size += 1) {
    const parser = new SSEParser();
    const events = [];
    for (let i = 0; i < wire.length; i += size) events.push(...parser.feed(wire.slice(i, i + size)));
    expect(events).toEqual([
      { event: "accepted", data: '{"a":1}' },
      { event: "sentence", data: "x\ny" },
    ]);
  }
});

test("an unfinished event is not emitted", () => {
  const parser = new SSEParser();
  expect(parser.feed("event: final\ndata: {}\n")).toEqual([]);
  expect(parser.feed("\n")).toEqual([{ event: "final", data: "{}" }]);
});
