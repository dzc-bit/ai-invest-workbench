import { useEffect, useState } from "react";
import { Eye, EyeOff, Settings, X } from "lucide-react";
import { AI_API_STYLE_LABELS, AI_RESEARCH_STYLES } from "../aiTypes";
import type { AiApiStyle, AiConfigUpdatePayload, AiConfigView, AiResearchStyle } from "../aiTypes";

type Props = {
  open: boolean;
  config: AiConfigView | null;
  isSaving?: boolean;
  errorMessage?: string | null;
  onClose: () => void;
  onSave: (payload: AiConfigUpdatePayload) => void;
  onRevealKey?: () => Promise<string>;
};

export function AiSettingsModal({ open, config, isSaving = false, errorMessage, onClose, onSave, onRevealKey }: Props) {
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState("");
  const [embeddingApiKey, setEmbeddingApiKey] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [keyVisible, setKeyVisible] = useState(false);
  const [apiStyle, setApiStyle] = useState<AiApiStyle>("chat-completions");
  const [researchStyle, setResearchStyle] = useState<AiResearchStyle>("balanced");
  const [temperature, setTemperature] = useState(0.3);
  const [maxSteps, setMaxSteps] = useState(8);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [insightsEnabled, setInsightsEnabled] = useState(true);
  const [insightMaxPerHour, setInsightMaxPerHour] = useState(6);
  const [reportEnabled, setReportEnabled] = useState(false);
  const [reportTime, setReportTime] = useState("15:30");
  const [evolutionEnabled, setEvolutionEnabled] = useState(false);
  const [evolutionTime, setEvolutionTime] = useState("16:00");

  useEffect(() => {
    if (!open || !config) {
      return;
    }
    setBaseUrl(config.base_url);
    setModel(config.model);
    setEmbeddingModel(config.embedding_model);
    setEmbeddingBaseUrl(config.embedding_base_url ?? "");
    setEmbeddingApiKey("");
    setApiKey("");
    setKeyVisible(false);
    setApiStyle(config.api_style);
    setResearchStyle(config.research_style);
    setTemperature(config.temperature);
    setMaxSteps(config.max_steps);
    setMaxTokens(config.max_tokens);
    setInsightsEnabled(config.insights_enabled);
    setInsightMaxPerHour(config.insight_max_per_hour);
    setReportEnabled(config.report_enabled);
    setReportTime(config.report_time || "15:30");
    setEvolutionEnabled(config.evolution_enabled);
    setEvolutionTime(config.evolution_time || "16:00");
  }, [open, config]);

  const toggleKeyVisible = async () => {
    if (keyVisible) {
      setKeyVisible(false);
      return;
    }
    if (onRevealKey) {
      try {
        setApiKey(await onRevealKey());
      } catch {
        // 显示失败时退回普通输入
      }
    }
    setKeyVisible(true);
  };

  if (!open) {
    return null;
  }

  const submit = () => {
    onSave({
      base_url: baseUrl.trim(),
      model: model.trim(),
      embedding_model: embeddingModel.trim(),
      embedding_base_url: embeddingBaseUrl.trim(),
      embedding_api_key: embeddingApiKey.trim(),
      api_key: apiKey.trim(),
      api_style: apiStyle,
      research_style: researchStyle,
      temperature: Number.isFinite(temperature) ? temperature : 0.3,
      max_steps: Number.isFinite(maxSteps) ? maxSteps : 8,
      max_tokens: Number.isFinite(maxTokens) ? maxTokens : 4096,
      insights_enabled: insightsEnabled,
      insight_max_per_hour: Number.isFinite(insightMaxPerHour) ? insightMaxPerHour : 6,
      report_enabled: reportEnabled,
      report_time: reportTime.trim() || "15:30",
      evolution_enabled: evolutionEnabled,
      evolution_time: evolutionTime.trim() || "16:00"
    });
  };

  return (
    <div className="modal-backdrop">
      <section className="ai-settings-modal" role="dialog" aria-modal="true" aria-label="AI 服务设置">
        <div className="modal-head">
          <div>
            <span className="section-kicker">OpenAI 兼容接口</span>
            <h2>AI 服务设置</h2>
          </div>
          <button className="icon-button" type="button" aria-label="关闭 AI 设置" onClick={onClose}>
            <X size={18} aria-hidden="true" />
          </button>
        </div>
        <div className="ai-settings-body">
          <label className="ai-field">
            <span>Base URL</span>
            <input
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://api.deepseek.com/v1"
              autoComplete="off"
            />
          </label>
          <label className="ai-field">
            <span>模型名称</span>
            <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="deepseek-chat" autoComplete="off" />
          </label>
          <label className="ai-field">
            <span>API 协议格式</span>
            <select value={apiStyle} onChange={(event) => setApiStyle(event.target.value as AiApiStyle)}>
              {(Object.keys(AI_API_STYLE_LABELS) as AiApiStyle[]).map((style) => (
                <option key={style} value={style}>
                  {AI_API_STYLE_LABELS[style]}
                </option>
              ))}
            </select>
          </label>
          <div className="ai-field">
            <span>研究风格（分析师人格）</span>
            <div className="ai-style-options" role="radiogroup" aria-label="研究风格">
              {AI_RESEARCH_STYLES.map((style) => (
                <button
                  key={style.value}
                  type="button"
                  role="radio"
                  aria-checked={researchStyle === style.value}
                  className={`ai-style-option ${researchStyle === style.value ? "active" : ""}`}
                  onClick={() => setResearchStyle(style.value)}
                >
                  <strong>{style.label}</strong>
                  <span>{style.description}</span>
                  <em className="ai-style-sample">例：{style.sample}</em>
                </button>
              ))}
            </div>
          </div>
          <div className="ai-field">
            <span className="ai-field-label-row">
              API Key {config?.api_key_masked ? `（已配置 ${config.api_key_masked}，留空保持不变）` : ""}
              {onRevealKey ? (
                <button
                  className="ai-reveal-button"
                  type="button"
                  aria-label={keyVisible ? "隐藏 API Key" : "显示 API Key"}
                  onClick={() => void toggleKeyVisible()}
                >
                  {keyVisible ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
                  {keyVisible ? "隐藏" : "显示"}
                </button>
              ) : null}
            </span>
            <input
              type={keyVisible ? "text" : "password"}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={config?.api_key_masked ? "留空保持现有 Key" : "sk-..."}
              autoComplete="new-password"
            />
          </div>
          <label className="ai-field">
            <span>Embedding 模型（可选，用于知识库检索）</span>
            <input
              value={embeddingModel}
              onChange={(event) => setEmbeddingModel(event.target.value)}
              placeholder="BAAI/bge-m3"
              autoComplete="off"
            />
          </label>
          <label className="ai-field">
            <span>
              Embedding Base URL（可选
              {config?.embedding_base_url ? `，当前 ${config.embedding_base_url}，留空保持不变` : "，留空跟随上方主 Base URL"}）
            </span>
            <input
              value={embeddingBaseUrl}
              onChange={(event) => setEmbeddingBaseUrl(event.target.value)}
              placeholder="https://api.siliconflow.cn/v1"
              autoComplete="off"
            />
          </label>
          <label className="ai-field">
            <span>
              Embedding API Key（可选
              {config?.embedding_api_key_masked ? `，已配置 ${config.embedding_api_key_masked}` : "，留空跟随主 API Key"}）
            </span>
            <input
              type="password"
              value={embeddingApiKey}
              onChange={(event) => setEmbeddingApiKey(event.target.value)}
              placeholder={config?.embedding_api_key_masked ? "留空保持现有 Key" : "sk-..."}
              autoComplete="new-password"
            />
          </label>
          <div className="ai-field">
            <span className="ai-field-label-row">定时任务（本地时间，桌面端运行期间生效）</span>
            <div className="ai-field-row">
              <label className="ai-checkbox">
                <input type="checkbox" checked={reportEnabled} onChange={(event) => setReportEnabled(event.target.checked)} />
                <span>定时复盘报告</span>
              </label>
              <input
                type="time"
                value={reportTime}
                onChange={(event) => setReportTime(event.target.value)}
                aria-label="复盘报告生成时间"
              />
            </div>
            <div className="ai-field-row">
              <label className="ai-checkbox">
                <input
                  type="checkbox"
                  checked={evolutionEnabled}
                  onChange={(event) => setEvolutionEnabled(event.target.checked)}
                />
                <span>策略库自动体检（含过拟合检测）</span>
              </label>
              <input
                type="time"
                value={evolutionTime}
                onChange={(event) => setEvolutionTime(event.target.value)}
                aria-label="策略体检运行时间"
              />
            </div>
            <small className="ai-settings-note">报告生成后保存在“运行产物/AI报告”，可在 AI 助手面板下载。</small>
          </div>
          <div className="ai-field-row">
            <label className="ai-field">
              <span>单次问题最大工具步数</span>
              <input
                type="number"
                min={1}
                max={16}
                value={maxSteps}
                onChange={(event) => setMaxSteps(Number(event.target.value))}
              />
            </label>
            <label className="ai-field">
              <span>每小时 AI 快讯上限</span>
              <input
                type="number"
                min={0}
                max={60}
                value={insightMaxPerHour}
                onChange={(event) => setInsightMaxPerHour(Number(event.target.value))}
              />
            </label>
          </div>
          <div className="ai-field-row">
            <label className="ai-field">
              <span>单次回答长度上限（tokens）</span>
              <input
                type="number"
                min={256}
                max={32000}
                step={256}
                value={maxTokens}
                onChange={(event) => setMaxTokens(Number(event.target.value))}
              />
            </label>
          </div>
          <label className="ai-checkbox">
            <input type="checkbox" checked={insightsEnabled} onChange={(event) => setInsightsEnabled(event.target.checked)} />
            <span>启用 AI 快讯推送（规则触发 + 节流，内容标注为 AI 生成）</span>
          </label>
          <p className="ai-settings-note">配置保存在本地用户数据目录（运行产物/AI配置），不会进入 Git 仓库。</p>
          {errorMessage ? <div className="error-banner" role="alert">{errorMessage}</div> : null}
        </div>
        <div className="ai-settings-actions">
          <button className="secondary-button" type="button" onClick={onClose}>
            取消
          </button>
          <button className="primary-button" type="button" onClick={submit} disabled={isSaving}>
            <Settings size={15} aria-hidden="true" />
            {isSaving ? "保存中" : "保存配置"}
          </button>
        </div>
      </section>
    </div>
  );
}
