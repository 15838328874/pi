import { describe, expect, it } from "vitest";
import { parseFrame } from "../src/api/sse";

/**
 * Frame splitting is the one part of the client a typechecker cannot validate:
 * the SSE rules live in prose in the spec and in the server's f-string, and
 * there is no browser here to catch a parser that silently drops a field.
 */
describe("parseFrame", () => {
  it("reads a frame the server actually wrote", () => {
    // Verbatim from a captured run against fake/demo.
    expect(parseFrame('event: start\ndata: {"session": "72a9eb32d988", "model": "fake/demo"}')).toEqual({
      event: "start",
      data: '{"session": "72a9eb32d988", "model": "fake/demo"}',
    });
  });

  it("defaults the event name to message", () => {
    expect(parseFrame('data: {"text":"hi"}')).toEqual({ event: "message", data: '{"text":"hi"}' });
  });

  it("strips exactly one leading space after the colon", () => {
    // Two spaces means the value starts with a space; eating both would corrupt
    // any payload whose first character is significant.
    expect(parseFrame("data:  padded")?.data).toBe(" padded");
    expect(parseFrame("data:tight")?.data).toBe("tight");
  });

  it("joins multiple data lines with a newline", () => {
    expect(parseFrame("data: line1\ndata: line2")?.data).toBe("line1\nline2");
  });

  it("ignores comment lines, which is how a keep-alive arrives", () => {
    expect(parseFrame(": ping\nevent: text_delta\ndata: x")).toEqual({
      event: "text_delta",
      data: "x",
    });
  });

  it("returns null for a frame carrying nothing", () => {
    expect(parseFrame(": ping")).toBeNull();
    expect(parseFrame("")).toBeNull();
  });

  it("accepts CRLF, which a proxy is allowed to normalise to", () => {
    expect(parseFrame("event: done\r\ndata: {}")).toEqual({ event: "done", data: "{}" });
  });

  it("keeps a non-default event even with no data", () => {
    // `event: done` with `data: {}` is the real form, but a bare event line is
    // still a frame the caller may care about - only a plain "message" with no
    // data is worth dropping.
    expect(parseFrame("event: done")).toEqual({ event: "done", data: "" });
  });

  it("ignores a field line with no colon", () => {
    expect(parseFrame("garbage\ndata: ok")?.data).toBe("ok");
  });
});
