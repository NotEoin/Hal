// The Hal extension: commands that talk to a running `hal code` through channel.ts.
//
// It reads the editor state the user asks for (selection, diagnostics, ambient file when enabled)
// and sends it; it runs nothing the terminal session sends back. The only thing it does with a
// reply is open a diff of two files that the host named, after checking they stay inside the
// two roots the host reported.
//
// `hal.command` is typed into a shell, so it is a machine-scoped setting (a repository's
// .vscode/settings.json cannot override it) and must be a plain command path with no shell syntax.
import * as path from "path";
import * as vscode from "vscode";
import { Channel, Incoming } from "./channel";

const MAX_DIFF_FILES = 20; // a diff tab per file; more is noise
const CONTEXT_DELAY_MS = 300; // selection events come in bursts
const SEVERITY = ["error", "warning", "info", "hint"];
const SAFE_COMMAND = /^[A-Za-z0-9_.\/~+-]+$/; // a path or program name; no spaces, quotes or ; & | $ `

function workspaceFolder(): vscode.WorkspaceFolder | undefined {
  const active = vscode.window.activeTextEditor?.document.uri;
  return (active ? vscode.workspace.getWorkspaceFolder(active) : undefined) ?? vscode.workspace.workspaceFolders?.[0];
}

/** True when `rel` is a plain relative path with no way out of its root. */
function safeRelative(rel: unknown): rel is string {
  return typeof rel === "string" && rel.length > 0 && !path.isAbsolute(rel) && !rel.split(/[\\/]/).includes("..");
}

async function showChanges(msg: Incoming): Promise<void> {
  const project = msg.project;
  const copy = msg.copy;
  const files = Array.isArray(msg.files) ? msg.files : [];
  if (typeof project !== "string" || typeof copy !== "string" || copy === "") {
    void vscode.window.showInformationMessage("Hal: no changes to show.");
    return;
  }
  if (files.length === 0) {
    void vscode.window.showInformationMessage("Hal: the copy has no changes yet.");
    return;
  }
  for (const rel of files.slice(0, MAX_DIFF_FILES)) {
    if (!safeRelative(rel)) {
      continue;
    }
    const left = vscode.Uri.file(path.join(project, rel));
    const right = vscode.Uri.file(path.join(copy, rel));
    await vscode.commands.executeCommand("vscode.diff", left, right, `${rel} (repo <-> Hal copy)`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const channel = new Channel((msg) => {
    if (msg.type === "changes") {
      void showChanges(msg);
    } else if (msg.type === "request_diagnostics") {
      void sendDiagnostics();
    } else if (msg.type === "error") {
      void vscode.window.showWarningMessage(`Hal: ${String(msg.message)}`);
    }
  });

  async function ensure(): Promise<boolean> {
    if (channel.connected) {
      return true;
    }
    const folder = workspaceFolder();
    if (folder !== undefined && (await channel.connect(folder.uri.fsPath))) {
      return true;
    }
    void vscode.window.showInformationMessage('No Hal session for this folder. Run "Hal: Open in terminal" first.');
    return false;
  }

  function relative(uri: vscode.Uri): string | undefined {
    const folder = vscode.workspace.getWorkspaceFolder(uri);
    return folder ? path.relative(folder.uri.fsPath, uri.fsPath) : undefined;
  }

  async function sendDiagnostics(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    const rel = editor ? relative(editor.document.uri) : undefined;
    if (!editor || rel === undefined || !(await ensure())) {
      return;
    }
    const items = vscode.languages.getDiagnostics(editor.document.uri).map((d) => ({
      line: d.range.start.line + 1,
      severity: SEVERITY[d.severity] ?? "info",
      message: d.message,
    }));
    channel.send({ type: "diagnostics", path: rel, items });
  }

  function pushContext(): void {
    const editor = vscode.window.activeTextEditor;
    if (!channel.connected || !vscode.workspace.getConfiguration("hal").get<boolean>("shareActiveFile")) {
      return;
    }
    const rel = editor ? relative(editor.document.uri) : undefined;
    const sel = editor && !editor.selection.isEmpty ? { start: editor.selection.start.line + 1, end: editor.selection.end.line + 1 } : null;
    channel.send({ type: "context", path: rel ?? null, selection: sel });
  }

  let timer: NodeJS.Timeout | undefined;
  const soon = (): void => {
    clearTimeout(timer);
    timer = setTimeout(pushContext, CONTEXT_DELAY_MS);
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("hal.openInTerminal", () => {
      const folder = workspaceFolder();
      if (folder === undefined) {
        void vscode.window.showWarningMessage("Hal: open a folder first.");
        return;
      }
      const command = vscode.workspace.getConfiguration("hal").inspect<string>("command")?.globalValue ?? "hal";
      if (!SAFE_COMMAND.test(command)) {
        void vscode.window.showErrorMessage("Hal: hal.command must be a plain program path without spaces or shell syntax.");
        return;
      }
      const terminal = vscode.window.createTerminal({ name: "Hal", cwd: folder.uri.fsPath });
      terminal.show();
      terminal.sendText(`${command} code`, true);
    }),
    vscode.commands.registerCommand("hal.addSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      const rel = editor ? relative(editor.document.uri) : undefined;
      if (!editor || rel === undefined || !(await ensure())) {
        return;
      }
      const sel = editor.selection;
      const end = sel.isEmpty || sel.end.character > 0 ? sel.end.line : sel.end.line - 1;
      channel.send({ type: "add_selection", path: rel, start: sel.start.line + 1, end: end + 1 });
    }),
    vscode.commands.registerCommand("hal.sendDiagnostics", sendDiagnostics),
    vscode.commands.registerCommand("hal.showChanges", async () => {
      if (await ensure()) {
        channel.send({ type: "show_changes" });
      }
    }),
    vscode.commands.registerCommand("hal.acceptChanges", async () => {
      if (await ensure()) {
        channel.send({ type: "accept" });
        void vscode.window.showInformationMessage("Hal: confirm the accept in the terminal.");
      }
    }),
    vscode.window.onDidChangeActiveTextEditor(soon),
    vscode.window.onDidChangeTextEditorSelection(soon),
    { dispose: () => channel.close() },
  );
}

export function deactivate(): void {}
