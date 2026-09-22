import { describe, expect, it, vi } from "vitest";

const invokeMock = vi.hoisted(() => vi.fn());
const updaterCheckMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock
}));

vi.mock("@tauri-apps/plugin-updater", () => ({
  check: updaterCheckMock
}));
import {
  checkWithUpdatePreflight,
  createTauriUpdateApi,
  isRetryableUpdateError,
  retryUpdateCheck,
  translateUpdateError,
  type UpdatePreflight
} from "./updateClient";

describe("createTauriUpdateApi", () => {
  it("uses the package version in browser preview", async () => {
    const api = createTauriUpdateApi();

    await expect(api.getVersion()).resolves.toBe("1.5.2");
  });

  it("retries transport failures but not signature failures", async () => {
    expect(isRetryableUpdateError(new Error("failed to fetch update: timeout"))).toBe(true);
    expect(isRetryableUpdateError(new Error("signature verification failed"))).toBe(false);

    let attempts = 0;
    await expect(
      retryUpdateCheck(async () => {
        attempts += 1;
        if (attempts === 1) {
          throw new Error("failed to fetch update: timeout");
        }
        return "ok";
      })
    ).resolves.toBe("ok");
    expect(attempts).toBe(2);

    attempts = 0;
    await expect(
      retryUpdateCheck(async () => {
        attempts += 1;
        throw new Error("signature verification failed");
      })
    ).rejects.toThrow("signature verification failed");
    expect(attempts).toBe(1);
  });

  it("retries reqwest request-send failures as network errors", async () => {
    const error = new Error("error sending request for url (https://updates.example.test/latest.json)");

    expect(isRetryableUpdateError(error)).toBe(true);
    expect(translateUpdateError(error)).toBe("网络连接异常，暂时无法检查更新。");

    let attempts = 0;
    await expect(
      retryUpdateCheck(async () => {
        attempts += 1;
        if (attempts === 1) {
          throw error;
        }
        return "ok";
      })
    ).resolves.toBe("ok");
    expect(attempts).toBe(2);
  });

  it("retries Tauri-style HTTP 5xx errors but never HTTP 4xx errors", () => {
    expect(isRetryableUpdateError(new Error("HTTP status server error (501)"))).toBe(true);
    expect(isRetryableUpdateError(new Error("HTTP status server error (505)"))).toBe(true);
    expect(isRetryableUpdateError(new Error("HTTP status client error (404)"))).toBe(false);
    expect(isRetryableUpdateError(new Error("HTTP status client error (429)"))).toBe(false);
  });

  it("does not retry a Tauri ReleaseNotFound error merely because it mentions fetch", () => {
    expect(
      isRetryableUpdateError(new Error("ReleaseNotFound: failed to fetch a release from the remote server"))
    ).toBe(false);
  });

  it("prioritizes an explicit HTTP 5xx status over a not-found phrase", () => {
    expect(isRetryableUpdateError(new Error("HTTP 503 not found while fetching manifest"))).toBe(true);
  });

  it("retries only network and 5xx updater preflight failures before checking signed metadata", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    let preflightAttempts = 0;
    let pluginChecks = 0;

    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: 404 };
        },
        async () => {
          pluginChecks += 1;
          return "unexpected";
        }
      )
    ).rejects.toThrow("HTTP 404");
    expect(preflightAttempts).toBe(1);
    expect(pluginChecks).toBe(0);

    preflightAttempts = 0;
    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: preflightAttempts === 1 ? 503 : 200 };
        },
        async () => {
          pluginChecks += 1;
          return "signed-update";
        }
      )
    ).resolves.toBe("signed-update");
    expect(preflightAttempts).toBe(2);
    expect(pluginChecks).toBe(1);

    preflightAttempts = 0;
    pluginChecks = 0;
    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: 200 };
        },
        async () => {
          pluginChecks += 1;
          throw new Error("Could not fetch a valid release JSON from the remote");
        }
      )
    ).rejects.toThrow("Could not fetch a valid release JSON from the remote");
    expect(preflightAttempts).toBe(2);
    expect(pluginChecks).toBe(1);
  });

  it("retries a signed updater network failure after a successful native preflight", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    let preflightAttempts = 0;
    let signedAttempts = 0;

    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: 200 };
        },
        async () => {
          signedAttempts += 1;
          if (signedAttempts === 1) {
            throw new Error("network connection reset while checking update");
          }
          return "signed-update";
        }
      )
    ).resolves.toBe("signed-update");
    expect(preflightAttempts).toBe(2);
    expect(signedAttempts).toBe(2);
  });

  it("retries a signed updater 5xx failure after a successful native preflight", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    let preflightAttempts = 0;
    let signedAttempts = 0;

    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: 200 };
        },
        async () => {
          signedAttempts += 1;
          if (signedAttempts === 1) {
            throw new Error("HTTP 503 update service unavailable");
          }
          return "signed-update";
        }
      )
    ).resolves.toBe("signed-update");
    expect(preflightAttempts).toBe(2);
    expect(signedAttempts).toBe(2);
  });

  it("reclassifies an opaque signed updater error before retrying", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    const preflightResults: UpdatePreflight[] = [
      { kind: "http", endpoint, status: 200 },
      { kind: "http", endpoint, status: 503 },
      { kind: "http", endpoint, status: 200 }
    ];
    let preflightAttempts = 0;
    let signedAttempts = 0;

    await expect(
      checkWithUpdatePreflight(
        async () => {
          preflightAttempts += 1;
          const result = preflightResults.shift();
          if (!result) {
            throw new Error("missing preflight result");
          }
          return result;
        },
        async () => {
          signedAttempts += 1;
          if (signedAttempts === 1) {
            throw new Error("Could not fetch a valid release JSON from the remote");
          }
          return "signed-update";
        }
      )
    ).resolves.toBe("signed-update");
    expect(preflightAttempts).toBe(3);
    expect(signedAttempts).toBe(2);
  });

  it("does not retry an opaque signed updater error when re-preflight reports 404", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    const preflightResults: UpdatePreflight[] = [
      { kind: "http", endpoint, status: 200 },
      { kind: "http", endpoint, status: 404 }
    ];
    let preflightAttempts = 0;
    let signedAttempts = 0;

    await expect(
      checkWithUpdatePreflight(
        async () => {
          preflightAttempts += 1;
          const result = preflightResults.shift();
          if (!result) {
            throw new Error("missing preflight result");
          }
          return result;
        },
        async () => {
          signedAttempts += 1;
          throw new Error("Could not fetch a valid release JSON from the remote");
        }
      )
    ).rejects.toThrow("HTTP 404");
    expect(preflightAttempts).toBe(2);
    expect(signedAttempts).toBe(1);
  });

  it("keeps opaque signed updater metadata errors when re-preflight succeeds", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    const errors = [
      "Could not fetch a valid release JSON from the remote",
      "failed to fetch update manifest: invalid JSON"
    ];

    for (const message of errors) {
      let preflightAttempts = 0;
      let signedAttempts = 0;
      await expect(
        checkWithUpdatePreflight(
          async (): Promise<UpdatePreflight> => {
            preflightAttempts += 1;
            return { kind: "http", endpoint, status: 200 };
          },
          async () => {
            signedAttempts += 1;
            throw new Error(message);
          }
        )
      ).rejects.toThrow(message);
      expect(preflightAttempts).toBe(2);
      expect(signedAttempts).toBe(1);
    }
  });

  it("retries an explicit signed updater 5xx error even when it mentions an invalid manifest", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    let preflightAttempts = 0;
    let signedAttempts = 0;

    await expect(
      checkWithUpdatePreflight(
        async (): Promise<UpdatePreflight> => {
          preflightAttempts += 1;
          return { kind: "http", endpoint, status: 200 };
        },
        async () => {
          signedAttempts += 1;
          if (signedAttempts === 1) {
            throw new Error("HTTP 503 invalid update manifest");
          }
          return "signed-update";
        }
      )
    ).resolves.toBe("signed-update");
    expect(preflightAttempts).toBe(2);
    expect(signedAttempts).toBe(2);
  });

  it("does not retry explicit non-transport signed updater failures after a successful native preflight", async () => {
    const endpoint = "https://updates.example.test/latest.json";
    const errors = ["HTTP 404 update manifest missing", "signature verification failed"];

    for (const message of errors) {
      let preflightAttempts = 0;
      let signedAttempts = 0;
      await expect(
        checkWithUpdatePreflight(
          async (): Promise<UpdatePreflight> => {
            preflightAttempts += 1;
            return { kind: "http", endpoint, status: 200 };
          },
          async () => {
            signedAttempts += 1;
            throw new Error(message);
          }
        )
      ).rejects.toThrow(message);
      expect(preflightAttempts).toBe(1);
      expect(signedAttempts).toBe(1);
    }
  });

  it("uses the native preflight command before the signed updater in Tauri", async () => {
    const originalInternals = window.__TAURI_INTERNALS__;
    window.__TAURI_INTERNALS__ = {};
    invokeMock.mockResolvedValue({
      kind: "http",
      endpoint: "https://updates.example.test/latest.json",
      status: 404
    });

    try {
      await expect(createTauriUpdateApi().check()).rejects.toThrow("HTTP 404");
      expect(invokeMock).toHaveBeenCalledWith("updater_preflight");
      expect(updaterCheckMock).not.toHaveBeenCalled();
    } finally {
      window.__TAURI_INTERNALS__ = originalInternals;
      invokeMock.mockReset();
      updaterCheckMock.mockReset();
    }
  });

  it("does not mislabel an HTTP status error as a generic check failure", () => {
    expect(translateUpdateError(new Error("update manifest returned HTTP 503"))).toBe("更新服务暂时不可用，请稍后重试。");
  });
});
