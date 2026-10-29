// MaiBot OMP Bridge Extension
// Automatically loaded by OMP to report external session lifecycle events to MaiBot.
// Uses a 50ms timeout to ensure zero impact or delay on interactive terminal sessions.

import net from "node:net";

const SOCKET_PATH = process.env.MAIBOT_OMP_SOCKET || "/tmp/maibot-omp-bridge.sock";
const TIMEOUT_MS = 50;

interface SessionManagerLike {
  getSessionId?: () => string | undefined;
  getSessionFile?: () => string | undefined;
}

interface ExtensionContextLike {
  cwd?: string;
  sessionManager?: SessionManagerLike;
}

interface ExtensionToolCallEvent {
  toolName?: string;
  input?: Record<string, unknown>;
}

interface ExtensionAgentEndEvent {
  status?: string;
  error?: unknown;
}

interface ExtensionAPILike {
  on(event: "session_start" | "agent_start", handler: (event: unknown, ctx: ExtensionContextLike) => Promise<void> | void): void;
  on(event: "tool_call" | "tool_result", handler: (event: ExtensionToolCallEvent, ctx: ExtensionContextLike) => Promise<void> | void): void;
  on(event: "agent_end", handler: (event: ExtensionAgentEndEvent, ctx: ExtensionContextLike) => Promise<void> | void): void;
}

function sendEvent(payload: Record<string, unknown>): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  let settled = false;

  const finish = () => {
    if (!settled) {
      settled = true;
      resolve();
    }
  };

  const timer = setTimeout(() => {
    socket.destroy();
    finish();
  }, TIMEOUT_MS);
  timer.unref?.();

  const socket = net.createConnection(SOCKET_PATH);
  socket.on("error", () => {
    clearTimeout(timer);
    socket.destroy();
    finish();
  });

  socket.on("connect", () => {
    try {
      socket.write(`${JSON.stringify(payload)}\n`, () => {
        clearTimeout(timer);
        socket.end();
        finish();
      });
    } catch {
      clearTimeout(timer);
      socket.destroy();
      finish();
    }
  });

  return promise;
}

export default function maibotBridge(pi: ExtensionAPILike): void {
  let sessionId: string | undefined;
  let sessionPath: string | undefined;

  function updateSession(ctx: ExtensionContextLike): void {
    try {
      sessionId = ctx.sessionManager?.getSessionId?.();
    } catch {
      sessionId = undefined;
    }
    try {
      const p = ctx.sessionManager?.getSessionFile?.();
      sessionPath = typeof p === "string" ? p : undefined;
    } catch {
      sessionPath = undefined;
    }
  }

  pi.on("session_start", async (_event: unknown, ctx: ExtensionContextLike) => {
    updateSession(ctx);
    await sendEvent({
      type: "session_start",
      sessionId,
      sessionPath,
      cwd: ctx.cwd || process.cwd(),
      pid: process.pid,
      timestamp: Date.now(),
      isBotChild: process.env.MAIBOT_OMP_INTERNAL === "1",
    });
  });

  pi.on("agent_start", async (_event: unknown, ctx: ExtensionContextLike) => {
    updateSession(ctx);
    await sendEvent({
      type: "agent_start",
      sessionId,
      sessionPath,
      cwd: ctx.cwd || process.cwd(),
      pid: process.pid,
      timestamp: Date.now(),
      isBotChild: process.env.MAIBOT_OMP_INTERNAL === "1",
    });
  });

  pi.on("tool_call", async (event: ExtensionToolCallEvent, ctx: ExtensionContextLike) => {
    updateSession(ctx);
    if (event.toolName === "ask") {
      await sendEvent({
        type: "ask_start",
        sessionId,
        sessionPath,
        cwd: ctx.cwd || process.cwd(),
        pid: process.pid,
        input: event.input,
        timestamp: Date.now(),
        isBotChild: process.env.MAIBOT_OMP_INTERNAL === "1",
      });
    }
  });

  pi.on("tool_result", async (event: ExtensionToolCallEvent, ctx: ExtensionContextLike) => {
    updateSession(ctx);
    if (event.toolName === "ask") {
      await sendEvent({
        type: "ask_end",
        sessionId,
        sessionPath,
        cwd: ctx.cwd || process.cwd(),
        pid: process.pid,
        timestamp: Date.now(),
        isBotChild: process.env.MAIBOT_OMP_INTERNAL === "1",
      });
    }
  });

  pi.on("agent_end", async (event: ExtensionAgentEndEvent, ctx: ExtensionContextLike) => {
    updateSession(ctx);
    let previewContent: string | undefined;
    let finalStatus: string = event.status || "completed";
    let detectedError: string | undefined = event.error !== undefined ? String(event.error) : undefined;

    try {
      const msgs = (event as Record<string, unknown>).messages;
      if (Array.isArray(msgs)) {
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i];
          if (m && typeof m === "object" && (m as Record<string, unknown>).role === "assistant") {
            const rawMsg = m as Record<string, unknown>;
            const c = rawMsg.content;
            if (typeof c === "string" && c.trim()) {
              previewContent = c.trim();
              break;
            } else if (Array.isArray(c)) {
              for (const p of c) {
                if (p && typeof p === "object" && (p as Record<string, unknown>).type === "text") {
                  const t = (p as Record<string, unknown>).text;
                  if (typeof t === "string" && t.trim()) {
                    previewContent = t.trim();
                    break;
                  }
                }
              }
              if (previewContent) break;
            }
          }
        }
      }
    } catch {
      // ignore
    }

    if (previewContent && (previewContent.trim() === "** Error: Request was aborted**" || previewContent.trim().startsWith("** Error:"))) {
      finalStatus = "aborted";
      detectedError = detectedError || "Request was aborted";
    } else if (previewContent) {
      finalStatus = "completed";
      detectedError = undefined;
    }

    await sendEvent({
      type: "agent_end",
      sessionId,
      sessionPath,
      cwd: ctx.cwd || process.cwd(),
      pid: process.pid,
      status: finalStatus,
      error: detectedError,
      content: previewContent ? previewContent.slice(0, 100) : undefined,
      timestamp: Date.now(),
      isBotChild: process.env.MAIBOT_OMP_INTERNAL === "1",
    });
  });
}
