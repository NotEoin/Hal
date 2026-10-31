// The connection to a running `hal code` session on this machine.
//
// Design: the session creates <runtime>/hal/ide/<session>.sock (0600) and a .lock file that names
// its workspace. This module finds the lock for a workspace folder and speaks newline-delimited
// JSON over that Unix socket. It sends only the message types the host accepts (see
// packages/hal-core/src/halcore/ide/protocol.py) and never evaluates anything it receives.
import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";

const MAX_LINE = 1 << 20; // the host drops a client that sends more than 1 MiB per message

export interface Incoming {
  type?: unknown;
  [key: string]: unknown;
}

/** Folder where the host keeps sockets and lock files. */
export function ideDir(): string {
  const override = process.env.HAL_RUNTIME_DIR;
  const base = override ?? (process.env.XDG_RUNTIME_DIR ? path.join(process.env.XDG_RUNTIME_DIR, "hal") : path.join("/run/user", String(os.userInfo().uid), "hal"));
  return path.join(base, "ide");
}

/** The socket path of a live session for this workspace folder, or undefined. */
export function findSocket(workspace: string, dir: string = ideDir()): string | undefined {
  let real: string;
  try {
    real = fs.realpathSync(workspace);
  } catch {
    return undefined;
  }
  let names: string[];
  try {
    names = fs.readdirSync(dir).filter((n) => n.endsWith(".lock")).sort();
  } catch {
    return undefined;
  }
  for (const name of names) {
    try {
      const info = JSON.parse(fs.readFileSync(path.join(dir, name), "utf8")) as { workspace?: string; socket?: string };
      if (info.workspace === real && typeof info.socket === "string" && fs.existsSync(info.socket)) {
        return info.socket;
      }
    } catch {
      continue;
    }
  }
  return undefined;
}

/** One editor connection; `send` fails quietly when no session is running. */
export class Channel {
  private sock: net.Socket | undefined;
  private buffer = "";

  constructor(private readonly onMessage: (msg: Incoming) => void) {}

  get connected(): boolean {
    return this.sock !== undefined && !this.sock.destroyed;
  }

  /** Connects to the session of `workspace`; resolves false when there is none. */
  connect(workspace: string): Promise<boolean> {
    const socketPath = findSocket(workspace);
    if (socketPath === undefined) {
      return Promise.resolve(false);
    }
    return new Promise((resolve) => {
      const sock = net.createConnection(socketPath);
      sock.setEncoding("utf8");
      sock.once("connect", () => {
        this.sock = sock;
        this.send({ type: "hello", client: "vscode" });
        resolve(true);
      });
      sock.once("error", () => resolve(false));
      sock.on("close", () => {
        this.sock = undefined;
      });
      sock.on("data", (chunk: string) => this.feed(chunk));
    });
  }

  private feed(chunk: string): void {
    this.buffer += chunk;
    if (this.buffer.length > MAX_LINE) {
      this.close();
      return;
    }
    let at = this.buffer.indexOf("\n");
    while (at >= 0) {
      const line = this.buffer.slice(0, at);
      this.buffer = this.buffer.slice(at + 1);
      try {
        const parsed: unknown = JSON.parse(line);
        if (typeof parsed === "object" && parsed !== null) {
          this.onMessage(parsed as Incoming);
        }
      } catch {
        // a line that is not JSON is ignored
      }
      at = this.buffer.indexOf("\n");
    }
  }

  /** Sends one message object; returns false when not connected. */
  send(message: Record<string, unknown>): boolean {
    if (!this.connected || this.sock === undefined) {
      return false;
    }
    this.sock.write(JSON.stringify(message) + "\n");
    return true;
  }

  close(): void {
    this.sock?.destroy();
    this.sock = undefined;
  }
}
