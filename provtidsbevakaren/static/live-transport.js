"use strict";

(function exposeLiveTransport(global) {
  class LiveTransport {
    constructor(options) {
      this.options = options;
      this.enabled = false;
      this.eventSource = null;
      this.controller = null;
      this.timer = null;
      this.recovering = false;
      this.preferFetch = false;
      this.failures = 0;
      this.generation = 0;
    }

    start() {
      if (this.enabled && (this.eventSource || this.controller || this.timer || this.recovering)) {
        return;
      }
      this.enabled = true;
      this._connect();
    }

    resume() {
      if (!this.enabled) return;
      this.generation += 1;
      this.recovering = false;
      this._clearConnection();
      this._connect();
    }

    offline() {
      if (!this.enabled) return;
      this.generation += 1;
      this.recovering = false;
      this._clearConnection();
      this.options.onState("offline");
    }

    stop() {
      this.enabled = false;
      this.recovering = false;
      this.generation += 1;
      this._clearConnection();
    }

    _clearConnection() {
      this.options.clearTimeout(this.timer);
      this.timer = null;
      if (this.eventSource) this.eventSource.close();
      this.eventSource = null;
      if (this.controller) this.controller.abort();
      this.controller = null;
    }

    _connect() {
      if (!this.enabled || !this.options.isOnline()) {
        if (this.enabled) this.options.onState("offline");
        return;
      }
      this._clearConnection();
      this.options.onState("reconnecting");
      if (!this.preferFetch && this.options.EventSource) this._connectEventSource();
      else this._connectFetchStream();
    }

    _connectEventSource() {
      const source = new (this.options.EventSource)(this._streamUrl());
      this.eventSource = source;
      source.onopen = () => {
        if (this.eventSource !== source) return;
        this.options.onState("live");
      };
      source.onmessage = (event) => {
        if (this.eventSource !== source) return;
        this.options.onSnapshot(JSON.parse(event.data));
      };
      source.addEventListener("auth", () => this._unauthorized());
      source.onerror = () => {
        if (this.eventSource !== source) return;
        source.close();
        this.eventSource = null;
        this.preferFetch = true;
        this._recover();
      };
    }

    async _connectFetchStream() {
      const generation = ++this.generation;
      const controller = new this.options.AbortController();
      this.controller = controller;
      try {
        const response = await this.options.fetch(this._streamUrl(), {
          credentials: "same-origin",
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
        });
        if (response.status === 401) return this._unauthorized();
        if (!response.ok || !response.body?.getReader) throw new Error(`HTTP ${response.status}`);
        if (!this.enabled || generation !== this.generation) return;
        this.failures = 0;
        this.options.onState("live");
        const reader = response.body.getReader();
        const decoder = new this.options.TextDecoder();
        let buffer = "";
        while (this.enabled && generation === this.generation) {
          const { done, value } = await reader.read();
          if (done) throw new Error("Live stream closed");
          buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
          const blocks = buffer.split("\n\n");
          buffer = blocks.pop() || "";
          for (const block of blocks) {
            if (block.startsWith("event: auth")) return this._unauthorized();
            const data = block
              .split("\n")
              .filter((line) => line.startsWith("data: "))
              .map((line) => line.slice(6))
              .join("\n");
            if (data) {
              this.failures = 0;
              this.options.onSnapshot(JSON.parse(data));
            }
          }
        }
      } catch (error) {
        if (error.name !== "AbortError" && this.enabled && generation === this.generation) {
          this.controller = null;
          await this._recover();
        }
      } finally {
        if (this.controller === controller) this.controller = null;
      }
    }

    async _recover() {
      if (!this.enabled || this.recovering) return;
      const generation = this.generation;
      this.recovering = true;
      this.options.onState(this.options.isOnline() ? "reconnecting" : "offline");
      try {
        const response = await this.options.fetch(this._snapshotUrl(), {
          credentials: "same-origin",
        });
        if (response.status === 401) return this._unauthorized();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (generation !== this.generation) return;
        this.options.onSnapshot(await response.json());
      } catch (error) {
        if (!this.enabled) return;
      } finally {
        if (generation === this.generation) this.recovering = false;
      }
      if (generation !== this.generation) return;
      this._scheduleReconnect();
    }

    _scheduleReconnect() {
      if (!this.enabled || !this.options.isOnline()) return;
      this.failures += 1;
      const delay = Math.min(60000, 2000 * 2 ** (this.failures - 1));
      this.options.clearTimeout(this.timer);
      this.timer = this.options.setTimeout(() => {
        this.timer = null;
        this._connect();
      }, delay);
    }

    _unauthorized() {
      this.stop();
      this.options.onUnauthorized();
    }

    _streamUrl() {
      const base = this.options.streamUrl || "/api/live/stream";
      return `${base}?after=${this.options.getCursor()}`;
    }

    _snapshotUrl() {
      const base = this.options.snapshotUrl || "/api/live";
      return `${base}?after=${this.options.getCursor()}`;
    }
  }

  global.LiveTransport = LiveTransport;
  if (typeof module !== "undefined") module.exports = { LiveTransport };
})(typeof window === "undefined" ? globalThis : window);
