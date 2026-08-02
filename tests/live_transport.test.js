"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { LiveTransport } = require("../provtidsbevakaren/static/live-transport.js");

class MockEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    MockEventSource.instances.push(this);
  }

  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }

  close() {
    this.closed = true;
  }
}

function harness(overrides = {}) {
  const states = [];
  const snapshots = [];
  const timers = [];
  let unauthorized = 0;
  const transport = new LiveTransport({
    EventSource: MockEventSource,
    AbortController,
    TextDecoder,
    fetch: async () => {
      throw new Error("unexpected fetch");
    },
    setTimeout: (callback, delay) => {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout: (timer) => {
      if (timer) timer.cleared = true;
    },
    isOnline: () => true,
    getCursor: () => 7,
    onState: (value) => states.push(value),
    onSnapshot: (value) => snapshots.push(value),
    onUnauthorized: () => {
      unauthorized += 1;
    },
    ...overrides,
  });
  return { transport, states, snapshots, timers, unauthorized: () => unauthorized };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("starts one EventSource and delivers live status updates", () => {
  MockEventSource.instances = [];
  const context = harness();
  context.transport.start();
  context.transport.start();
  assert.equal(MockEventSource.instances.length, 1);
  const source = MockEventSource.instances[0];
  assert.equal(source.url, "/api/live/stream?after=7");
  source.onopen();
  source.onmessage({ data: '{"state":"authentication","events":[]}' });
  assert.deepEqual(context.states, ["reconnecting", "live"]);
  assert.equal(context.snapshots[0].state, "authentication");
});

test("falls back to one fetch stream after EventSource failure", async () => {
  MockEventSource.instances = [];
  const encoder = new TextEncoder();
  const calls = [];
  let readCount = 0;
  const context = harness({
    fetch: async (url) => {
      calls.push(url);
      if (url.startsWith("/api/live?")) {
        return { ok: true, status: 200, json: async () => ({ state: "idle", events: [] }) };
      }
      return {
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: async () => {
              readCount += 1;
              if (readCount === 1) {
                return {
                  done: false,
                  value: encoder.encode('data: {"state":"authenticated","events":[]}\n\n'),
                };
              }
              return new Promise(() => {});
            },
          }),
        },
      };
    },
  });
  context.transport.start();
  MockEventSource.instances[0].onerror();
  await flush();
  assert.deepEqual(calls, ["/api/live?after=7"]);
  assert.equal(context.timers.at(-1).delay, 2000);
  context.timers.at(-1).callback();
  await flush();
  assert.equal(calls.filter((url) => url.includes("/stream")).length, 1);
  assert.equal(context.snapshots.at(-1).state, "authenticated");
  assert.equal(context.states.at(-1), "live");
});

test("recovery uses exponential backoff instead of constant requests", async () => {
  const context = harness({
    EventSource: null,
    fetch: async () => {
      throw new Error("offline");
    },
  });
  context.transport.start();
  await flush();
  assert.equal(context.timers.at(-1).delay, 2000);
  context.timers.at(-1).callback();
  await flush();
  assert.equal(context.timers.at(-1).delay, 4000);
  context.timers.at(-1).callback();
  await flush();
  assert.equal(context.timers.at(-1).delay, 8000);
});

test("visibility resume replaces the connection and auth stops reconnects", () => {
  MockEventSource.instances = [];
  const context = harness();
  context.transport.start();
  const first = MockEventSource.instances[0];
  context.transport.resume();
  assert.equal(first.closed, true);
  assert.equal(MockEventSource.instances.length, 2);
  const second = MockEventSource.instances[1];
  second.listeners.auth();
  assert.equal(second.closed, true);
  assert.equal(context.unauthorized(), 1);
  assert.equal(context.transport.enabled, false);
  assert.equal(context.timers.filter((timer) => !timer.cleared).length, 0);
});
