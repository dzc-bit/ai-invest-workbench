import { invoke } from "@tauri-apps/api/core";
import type { ConditionGroup, ConditionNode, SavedStrategyPreset, StrategyConfig } from "./types";
import { defaultStrategy } from "./strategyDefaults";
import { isTauriRuntime } from "./tauriRuntime";

const STORAGE_KEY = "astock-saved-strategies";
const BUILTIN_ID_PREFIX = "builtin-";

const builtInStrategies: SavedStrategyPreset[] = [
  {
    id: "builtin-default",
    name: "基础均衡策略",
    saved_at: "builtin",
    strategy: cloneStrategyConfig(defaultStrategy)
  },
  {
    id: "builtin-breakout",
    name: "放量突破策略",
    saved_at: "builtin",
    strategy: {
      name: "放量突破策略",
      market_filters: [],
      entry_groups: [
        {
          id: "entry",
          operator: "and",
          conditions: [
            {
              id: "builtin-breakout-high",
              condition_id: "breakout_above_n_day_high",
              enabled: true,
              params: { window: 20 },
              data_lag_days: 0,
              expression: "突破20日新高"
            },
            {
              id: "builtin-breakout-volume",
              condition_id: "volume_ratio_between",
              enabled: true,
              params: { window: 2, min: 1.2, max: 2.5 },
              data_lag_days: 0,
              expression: "量比2日介于1.2到2.5"
            }
          ]
        }
      ],
      exit_rules: [
        {
          id: "builtin-breakout-exit",
          condition_id: "close_below_ma",
          enabled: true,
          params: { window: 3 },
          data_lag_days: 0,
          expression: "收盘价跌破3日均线"
        }
      ],
      score_threshold: null
    }
  },
  {
    id: "builtin-low-absorb",
    name: "回踩均线策略",
    saved_at: "builtin",
    strategy: {
      name: "回踩均线策略",
      market_filters: [],
      entry_groups: [
        {
          id: "entry",
          operator: "and",
          conditions: [
            {
              id: "builtin-pullback-ma",
              condition_id: "close_above_ma",
              enabled: true,
              params: { window: 20 },
              data_lag_days: 0,
              expression: "收盘价站上20日均线"
            },
            {
              id: "builtin-pullback-cap",
              condition_id: "market_cap_between",
              enabled: true,
              params: { min: 1000000000, max: 30000000000 },
              data_lag_days: 0,
              expression: "流通市值10亿到300亿"
            }
          ]
        }
      ],
      exit_rules: [
        {
          id: "builtin-pullback-exit",
          condition_id: "breakdown_below_n_day_low",
          enabled: true,
          params: { window: 20 },
          data_lag_days: 0,
          expression: "跌破20日低点"
        }
      ],
      score_threshold: null
    }
  }
];

function sortParams(params: ConditionNode["params"]): ConditionNode["params"] {
  return Object.fromEntries(Object.entries(params).sort(([left], [right]) => left.localeCompare(right)));
}

function normalizeCondition(condition: ConditionNode) {
  return {
    condition_id: condition.condition_id,
    enabled: condition.enabled,
    params: sortParams(condition.params),
    data_lag_days: condition.data_lag_days,
    expression: condition.expression ?? null,
    weight: condition.weight ?? null
  };
}

function normalizeGroup(group: ConditionGroup) {
  return {
    operator: group.operator,
    conditions: group.conditions.map(normalizeCondition)
  };
}

function uniqueSavedStrategyName(baseName: string, existingNames: string[]): string {
  const normalizedBase = baseName.trim() || "自定义策略";
  if (!existingNames.includes(normalizedBase)) {
    return normalizedBase;
  }
  let index = 2;
  while (existingNames.includes(`${normalizedBase}（${index}）`)) {
    index += 1;
  }
  return `${normalizedBase}（${index}）`;
}

export function cloneStrategyConfig(strategy: StrategyConfig): StrategyConfig {
  return JSON.parse(JSON.stringify(strategy)) as StrategyConfig;
}

export function strategySignature(strategy: StrategyConfig): string {
  return JSON.stringify({
    name: strategy.name.trim(),
    market_filters: strategy.market_filters.map(normalizeCondition),
    entry_groups: strategy.entry_groups.map(normalizeGroup),
    exit_rules: strategy.exit_rules.map(normalizeCondition),
    score_threshold: strategy.score_threshold ?? null
  });
}

export function hasSavableRules(strategy: StrategyConfig): boolean {
  return strategy.entry_groups.some((group) => group.conditions.length > 0) && strategy.exit_rules.length > 0;
}

function builtInStrategyPresets(): SavedStrategyPreset[] {
  return builtInStrategies.map((item) => ({
    ...item,
    strategy: cloneStrategyConfig(item.strategy)
  }));
}

function validateStrategyConfig(strategy: unknown, index: number): asserts strategy is StrategyConfig {
  if (!strategy || typeof strategy !== "object") {
    throw new Error(`已保存策略条目 #${index} 的 strategy 必须是对象。`);
  }
  const s = strategy as Record<string, unknown>;
  const validateCondition = (condition: unknown, path: string): void => {
    if (!condition || typeof condition !== "object" || Array.isArray(condition)) {
      throw new Error(`已保存策略条目 #${index} 的 ${path} 包含非法 condition。`);
    }
    const node = condition as Record<string, unknown>;
    if (
      typeof node.id !== "string" ||
      typeof node.condition_id !== "string" ||
      typeof node.enabled !== "boolean" ||
      !node.params ||
      typeof node.params !== "object" ||
      Array.isArray(node.params) ||
      typeof node.data_lag_days !== "number"
    ) {
      throw new Error(`已保存策略条目 #${index} 的 ${path} condition 字段不完整。`);
    }
  };
  if (!Array.isArray(s.market_filters)) {
    throw new Error(`已保存策略条目 #${index} 的 strategy.market_filters 必须是数组。`);
  }
  if (!Array.isArray(s.entry_groups)) {
    throw new Error(`已保存策略条目 #${index} 的 strategy.entry_groups 必须是数组。`);
  }
  if (!Array.isArray(s.exit_rules)) {
    throw new Error(`已保存策略条目 #${index} 的 strategy.exit_rules 必须是数组。`);
  }
  for (const condition of s.market_filters as unknown[]) {
    validateCondition(condition, "market_filters");
  }
  for (const group of s.entry_groups as unknown[]) {
    if (!group || typeof group !== "object" || Array.isArray(group)) {
      throw new Error(`已保存策略条目 #${index} 的 entry_groups 包含非法 group。`);
    }
    const value = group as Record<string, unknown>;
    if (
      typeof value.id !== "string" ||
      !["and", "or", "score"].includes(String(value.operator)) ||
      !Array.isArray(value.conditions)
    ) {
      throw new Error(`已保存策略条目 #${index} 的 entry_groups group 字段不完整。`);
    }
    for (const condition of value.conditions) {
      validateCondition(condition, "entry_groups.conditions");
    }
  }
  for (const rule of s.exit_rules as unknown[]) {
    validateCondition(rule, "exit_rules");
  }
}

function parseCustomSavedStrategies(raw: unknown): SavedStrategyPreset[] {
  if (!Array.isArray(raw)) {
    throw new Error("已保存策略数据格式错误：期望一个 JSON 数组。");
  }
  // Reject the whole payload if any entry is malformed instead of silently
  // dropping it. A partial parse would let a later save persist a strict subset
  // and permanently lose the dropped strategies.
  return raw.map((item, index) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      typeof item.name !== "string" ||
      typeof item.saved_at !== "string" ||
      !("strategy" in item)
    ) {
      throw new Error(`已保存策略数据存在非法条目（#${index}），已停止加载以避免覆盖丢失数据。`);
    }
    validateStrategyConfig(item.strategy, index);
    return {
      id: item.id,
      name: item.name,
      saved_at: item.saved_at,
      strategy: cloneStrategyConfig(item.strategy as StrategyConfig)
    };
  });
}

function loadSavedStrategiesFromLocalStorageStrict(): SavedStrategyPreset[] {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  const customItems = raw ? parseCustomSavedStrategies(JSON.parse(raw)) : [];
  return [...builtInStrategyPresets(), ...customItems];
}

function serializeCustomSavedStrategies(items: SavedStrategyPreset[]) {
  return items
    .filter((item) => !isBuiltInStrategyPreset(item.id))
    .map((item) => ({
      id: item.id,
      name: item.name,
      saved_at: item.saved_at,
      strategy: cloneStrategyConfig(item.strategy)
    }));
}

export function loadSavedStrategies(): SavedStrategyPreset[] {
  const builtins = builtInStrategyPresets();
  if (typeof window === "undefined" || isTauriRuntime()) {
    return builtins;
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return builtins;
    }
    const customItems = parseCustomSavedStrategies(JSON.parse(raw));
    return [...builtins, ...customItems];
  } catch {
    return builtins;
  }
}

export function persistSavedStrategies(items: SavedStrategyPreset[]): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(serializeCustomSavedStrategies(items)));
}

export async function loadSavedStrategiesFromStore(): Promise<SavedStrategyPreset[]> {
  if (!isTauriRuntime()) {
    return loadSavedStrategiesFromLocalStorageStrict();
  }
  const builtins = builtInStrategyPresets();
  const customItems = parseCustomSavedStrategies(await invoke<unknown[]>("load_saved_strategies"));
  return [...builtins, ...customItems];
}

export async function upsertSavedStrategyToStore(
  preset: SavedStrategyPreset,
  current: SavedStrategyPreset[]
): Promise<SavedStrategyPreset[]> {
  if (!isTauriRuntime()) {
    const next = [preset, ...current.filter((item) => item.id !== preset.id)];
    persistSavedStrategies(next);
    return next;
  }
  const customItems = parseCustomSavedStrategies(
    await invoke<unknown[]>("upsert_saved_strategy", { preset: serializeCustomSavedStrategies([preset])[0] })
  );
  return [...builtInStrategyPresets(), ...customItems];
}

export async function deleteSavedStrategyFromStore(
  presetId: string,
  current: SavedStrategyPreset[]
): Promise<SavedStrategyPreset[]> {
  if (!isTauriRuntime()) {
    const next = current.filter((item) => item.id !== presetId);
    persistSavedStrategies(next);
    return next;
  }
  const customItems = parseCustomSavedStrategies(
    await invoke<unknown[]>("delete_saved_strategy", { presetId })
  );
  return [...builtInStrategyPresets(), ...customItems];
}

export function isBuiltInStrategyPreset(presetId: string): boolean {
  return presetId.startsWith(BUILTIN_ID_PREFIX);
}

export function createSavedStrategyPreset(strategy: StrategyConfig, existing: SavedStrategyPreset[]): SavedStrategyPreset {
  const nextName = uniqueSavedStrategyName(strategy.name, existing.map((item) => item.name));
  return {
    id: `saved-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: nextName,
    saved_at: new Date().toISOString(),
    strategy: cloneStrategyConfig({
      ...strategy,
      name: nextName
    })
  };
}
