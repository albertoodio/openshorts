import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

/** Hold a Linux flock until release or renderer process death closes stdin. */
export async function acquireRenderSlot(
  lockPath: string | undefined = process.env.RENDER_RESOURCE_LOCK,
  waitSeconds: number = Number(process.env.RENDER_RESOURCE_WAIT_SECONDS ?? 300),
): Promise<() => Promise<void>> {
  if (!lockPath) return async () => {};
  if (!Number.isFinite(waitSeconds) || waitSeconds < 0) {
    throw new Error("Render resource wait must be finite and nonnegative");
  }
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  const holder = spawn("flock", ["-x", "-w", String(waitSeconds), lockPath,
    "sh", "-c", "printf 'ready\\n'; cat >/dev/null"], { stdio: ["pipe", "pipe", "pipe"] });
  // Always consume stderr; errors shown to the operator contain no file paths.
  holder.stderr.resume();
  const exited = new Promise<void>((resolve) => {
    holder.once("exit", () => resolve());
    holder.once("error", () => resolve());
  });
  try {
    await new Promise<void>((resolve, reject) => {
      holder.stdout.once("data", () => resolve());
      holder.once("error", () => reject(new Error("Render resource lock tool unavailable")));
      holder.once("exit", () => reject(new Error("Render resource is busy; retry after the current task finishes")));
    });
  } catch (error) {
    holder.stdin.end();
    await exited;
    throw error;
  }
  let released = false;
  return async (): Promise<void> => {
    if (released) return;
    released = true;
    holder.stdin.end();
    await exited;
  };
}
