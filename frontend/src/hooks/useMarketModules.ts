import { useCallback, useState } from "react";
import {
  loadClsFinance,
  loadMarketBriefing,
  loadMarketCommentary,
  loadMarketNews,
  loadNewsSummary,
  loadRecommendedStrategies,
  loadRiskAlerts
} from "../api";
import type {
  ClsFinanceResponse,
  DataServiceStatus,
  DatasetCoverage,
  MarketBriefingResponse,
  MarketCommentaryResponse,
  MarketNewsResponse,
  NewsSummaryResponse,
  RecommendedStrategy,
  RiskAlertsResponse
} from "../types";
import { useIndependentModuleRefresh } from "./useIndependentModuleRefresh";

/**
 * Owns the eight independent market-modules (commentary / news / finance /
 * summary / fupan / zaopan / risk / recommendations): their payloads, loading
 * flags, refresh cycle and manual refresh helpers.  Each module fails and
 * recovers on its own without affecting the others.  ``coverage`` participates
 * in the recommendations loader identity on purpose: a fresh coverage snapshot
 * must re-read the recommended strategies immediately.
 */
export function useMarketModules(dataService: DataServiceStatus | null, coverage: DatasetCoverage[]) {
  const [marketCommentary, setMarketCommentary] = useState<MarketCommentaryResponse | null>(null);
  const [marketNews, setMarketNews] = useState<MarketNewsResponse | null>(null);
  const [isLoadingNews, setIsLoadingNews] = useState(false);
  const [clsFinance, setClsFinance] = useState<ClsFinanceResponse | null>(null);
  const [isLoadingClsFinance, setIsLoadingClsFinance] = useState(false);
  const [newsSummary, setNewsSummary] = useState<NewsSummaryResponse | null>(null);
  const [isLoadingNewsSummary, setIsLoadingNewsSummary] = useState(false);
  const [fupanBriefing, setFupanBriefing] = useState<MarketBriefingResponse | null>(null);
  const [zaopanBriefing, setZaopanBriefing] = useState<MarketBriefingResponse | null>(null);
  const [riskAlerts, setRiskAlerts] = useState<RiskAlertsResponse | null>(null);
  const [isLoadingRiskAlerts, setIsLoadingRiskAlerts] = useState(false);
  const [recommendedStrategies, setRecommendedStrategies] = useState<RecommendedStrategy[]>([]);

  const loadCommentary = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      try {
        const response = await loadMarketCommentary(dataService.base_url);
        if (!isCancelled() && response) {
          setMarketCommentary(response);
        }
        return Boolean(response);
      } catch {
        // 行情评价失败独立恢复：前端保留最近一次评价（自带 updated_at）。
        return false;
      }
    },
    [dataService]
  );

  const loadNews = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      setIsLoadingNews(true);
      try {
        const response = await loadMarketNews(dataService.base_url);
        if (!isCancelled()) {
          setMarketNews(response);
        }
        return true;
      } catch {
        // Keep the last successful news list visible while this module retries.
        return false;
      } finally {
        if (!isCancelled()) {
          setIsLoadingNews(false);
        }
      }
    },
    [dataService]
  );

  const loadFupan = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      try {
        const response = await loadMarketBriefing(dataService.base_url, "fupan");
        if (!isCancelled() && response) {
          setFupanBriefing(response);
        }
        return Boolean(response);
      } catch {
        // Fupan keeps its latest independent result.
        return false;
      }
    },
    [dataService]
  );

  const loadZaopan = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      try {
        const response = await loadMarketBriefing(dataService.base_url, "zaopan");
        if (!isCancelled() && response) {
          setZaopanBriefing(response);
        }
        return Boolean(response);
      } catch {
        // Zaopan keeps its latest independent result.
        return false;
      }
    },
    [dataService]
  );

  const loadFinance = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      setIsLoadingClsFinance(true);
      try {
        const response = await loadClsFinance(dataService.base_url);
        if (!isCancelled()) {
          setClsFinance(response);
        }
        return true;
      } catch {
        // Finance data is independent from news and retains its last response.
        return false;
      } finally {
        if (!isCancelled()) {
          setIsLoadingClsFinance(false);
        }
      }
    },
    [dataService]
  );

  const loadSummary = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      setIsLoadingNewsSummary(true);
      try {
        const response = await loadNewsSummary(dataService.base_url);
        if (!isCancelled()) {
          setNewsSummary(response);
        }
        return true;
      } catch {
        // The summary is allowed to lag independently of the source news list.
        return false;
      } finally {
        if (!isCancelled()) {
          setIsLoadingNewsSummary(false);
        }
      }
    },
    [dataService]
  );

  const loadRiskAlertData = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      setIsLoadingRiskAlerts(true);
      try {
        const response = await loadRiskAlerts(dataService.base_url);
        if (!isCancelled()) {
          setRiskAlerts(response);
        }
        return true;
      } catch {
        // Preserve the last risk result when only this endpoint fails.
        return false;
      } finally {
        if (!isCancelled()) {
          setIsLoadingRiskAlerts(false);
        }
      }
    },
    [dataService]
  );

  const loadRecommendations = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      try {
        const response = await loadRecommendedStrategies(dataService.base_url);
        if (!isCancelled() && response) {
          setRecommendedStrategies(response.items);
        }
        return Boolean(response);
      } catch {
        // Recommendations remain independent when the cached coverage snapshot is refreshing.
        return false;
      }
    },
    [coverage, dataService]
  );

  const refreshNews = () => {
    void loadNews(() => false);
  };

  const refreshRiskAlerts = () => {
    void loadRiskAlertData(() => false);
  };

  useIndependentModuleRefresh(Boolean(dataService), loadCommentary);
  useIndependentModuleRefresh(Boolean(dataService), loadNews);
  useIndependentModuleRefresh(Boolean(dataService), loadFupan);
  useIndependentModuleRefresh(Boolean(dataService), loadZaopan);
  useIndependentModuleRefresh(Boolean(dataService), loadFinance);
  useIndependentModuleRefresh(Boolean(dataService), loadSummary);
  useIndependentModuleRefresh(Boolean(dataService), loadRiskAlertData);
  useIndependentModuleRefresh(Boolean(dataService), loadRecommendations);

  return {
    marketCommentary,
    marketNews,
    isLoadingNews,
    clsFinance,
    isLoadingClsFinance,
    newsSummary,
    isLoadingNewsSummary,
    fupanBriefing,
    zaopanBriefing,
    riskAlerts,
    isLoadingRiskAlerts,
    recommendedStrategies,
    refreshNews,
    refreshRiskAlerts
  };
}
