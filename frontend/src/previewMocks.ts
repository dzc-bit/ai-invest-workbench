// Mock 层只允许存在于开发/预览构建：生产包不该携带示例数据
// （dist 曾实测能搜到“示例股份 / sk-demo-key / 预览模式”，而 mockAiStatus
// 只列 4 个工具名、真实注册表是 20 个，漂移完全不可见）。动态 import +
// import.meta.env.DEV 让 Vite 在生产构建时把整个 mock 模块摇掉。
export async function previewApiMocks(): Promise<typeof import("./apiMocks") | null> {
  if (!import.meta.env.DEV) {
    return null;
  }
  return import("./apiMocks");
}

export async function previewAiMocks(): Promise<typeof import("./aiMocks") | null> {
  if (!import.meta.env.DEV) {
    return null;
  }
  return import("./aiMocks");
}
