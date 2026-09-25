import { getVersion } from "@tauri-apps/api/app";
import { invoke } from "@tauri-apps/api/core";
import { relaunch } from "@tauri-apps/plugin-process";
import { check } from "@tauri-apps/plugin-updater";
import type { DownloadEvent } from "@tauri-apps/plugin-updater";

declare global {
  const __APP_VERSION__: string;

  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export type InstallEvent =
  | { event: "Started"; data: { contentLength?: number } }
  | { event: "Progress"; data: { chunkLength: number } }
  | { event: "Finished" };

export interface AvailableUpdate {
  version: string;
  date?: string;
  body?: string;
  downloadAndInstall(onEvent?: (event: InstallEvent) => void): Promise<void>;
}

export interface UpdateApi {
  isRuntime(): boolean;
  getVersion(): Promise<string>;
  check(): Promise<AvailableUpdate | null>;
  relaunch(): Promise<void>;
}

// 运行时检测只有一个家（tauriRuntime.ts）：这里曾各自重写一遍，判断口径
// 漂移时浏览器预览与桌面端会静默分叉。
import { isTauriRuntime } from "./tauriRuntime";

export { isTauriRuntime };

function messageFrom(caught: unknown): string {
  if (caught instanceof Error) {
    return caught.message;
  }
  return String(caught ?? "");
}

export type UpdatePreflight =
  | { kind: "http"; endpoint: string; status: number }
  | { kind: "network"; endpoint: string; message: string };

function hasUpdateSignatureError(message: string): boolean {
  return /(signature|pubkey|public key|verify|verification|ed25519)/i.test(message);
}

function hasUpdateClientError(message: string): boolean {
  return /\b4\d\d\b/i.test(message);
}

function hasUpdateNotFoundError(message: string): boolean {
  return /not[\s_-]*found/i.test(message);
}

function hasUpdateServerError(message: string): boolean {
  return /\b5\d\d\b/i.test(message);
}

function hasUpdateMetadataParseError(message: string): boolean {
  return /(?:valid|invalid|malformed)\s+(?:release\s+json|(?:update\s+)?manifest)|(?:release\s+json|(?:update\s+)?manifest).*?(?:parse|parsing|invalid|malformed|schema|format)|(?:parse|parsing).*?(?:release\s+json|(?:update\s+)?manifest)/i.test(
    message
  );
}

function shouldReclassifySignedUpdateError(caught: unknown): boolean {
  const message = messageFrom(caught).toLowerCase();
  return (
    hasUpdateMetadataParseError(message) &&
    !hasUpdateSignatureError(message) &&
    !hasUpdateClientError(message) &&
    !hasUpdateNotFoundError(message) &&
    !hasUpdateServerError(message)
  );
}

export function isRetryableUpdateError(caught: unknown): boolean {
  const message = messageFrom(caught).toLowerCase();
  if (hasUpdateSignatureError(message)) {
    return false;
  }
  if (hasUpdateClientError(message)) {
    return false;
  }
  if (hasUpdateServerError(message)) {
    return true;
  }
  if (hasUpdateNotFoundError(message)) {
    return false;
  }
  if (hasUpdateMetadataParseError(message)) {
    return false;
  }
  return Boolean(
    /\b5\d\d\b|network|fetch|dns|timeout|timed out|connection|offline|resolve|host|sending request/i.test(
      message
    )
  );
}

export async function retryUpdateCheck<T>(operation: () => Promise<T>): Promise<T> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await operation();
    } catch (caught) {
      if (attempt === 1 || !isRetryableUpdateError(caught)) {
        throw caught;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }
  }
  throw new Error("update check did not complete");
}

function updatePreflightError(preflight: UpdatePreflight): Error {
  if (preflight.kind === "http") {
    return new Error(`update preflight returned HTTP ${preflight.status} for ${preflight.endpoint}`);
  }
  return new Error(`update preflight network failure for ${preflight.endpoint}: ${preflight.message}`);
}

export async function checkWithUpdatePreflight<T>(
  preflight: () => Promise<UpdatePreflight>,
  signedCheck: () => Promise<T>
): Promise<T> {
  return retryUpdateCheck(async () => {
    const result = await preflight();
    if (result.kind === "http" && result.status >= 200 && result.status < 300) {
      try {
        return await signedCheck();
      } catch (caught) {
        if (!shouldReclassifySignedUpdateError(caught)) {
          throw caught;
        }
        const reclassified = await preflight();
        if (reclassified.kind === "http" && reclassified.status >= 200 && reclassified.status < 300) {
          throw caught;
        }
        throw updatePreflightError(reclassified);
      }
    }
    throw updatePreflightError(result);
  });
}

export function translateUpdateError(caught: unknown): string {
  const message = messageFrom(caught).toLowerCase();

  if (/(signature|pubkey|public key|verify|verification|ed25519)/i.test(message)) {
    return "更新文件校验失败，请确认安装包来源可信后重试。";
  }
  if (/(network|fetch|dns|timeout|timed out|connection|offline|resolve|host|sending request)/i.test(message)) {
    return "网络连接异常，暂时无法检查更新。";
  }
  if (/(http\s*5\d\d|status(?: code)?\s*5\d\d|\b500\b|\b502\b|\b503\b|\b504\b)/i.test(message)) {
    return "更新服务暂时不可用，请稍后重试。";
  }
  if (/(endpoint|url|404|not found|manifest|latest\.json)/i.test(message)) {
    return "更新服务地址暂不可用，请稍后重试。";
  }
  if (/(install|permission|extract|rename|replace|write)/i.test(message)) {
    return "安装更新失败，请关闭占用程序后重试。";
  }
  return "检查更新失败，请稍后重试。";
}

function mapInstallEvent(event: DownloadEvent): InstallEvent {
  return event;
}

export function createTauriUpdateApi(): UpdateApi {
  return {
    isRuntime: isTauriRuntime,
    async getVersion() {
      if (!isTauriRuntime()) {
        return __APP_VERSION__;
      }
      return getVersion();
    },
    async check() {
      if (!isTauriRuntime()) {
        return null;
      }

      return checkWithUpdatePreflight(
        () => invoke<UpdatePreflight>("updater_preflight"),
        async () => {
          const update = await check({ timeout: 30000 });
          if (!update) {
            return null;
          }

          return {
            version: update.version,
            date: update.date,
            body: update.body,
            downloadAndInstall(onEvent) {
              return update.downloadAndInstall(
                onEvent ? (event) => onEvent(mapInstallEvent(event)) : undefined
              );
            }
          };
        }
      );
    },
    async relaunch() {
      if (isTauriRuntime()) {
        await relaunch();
      }
    }
  };
}
