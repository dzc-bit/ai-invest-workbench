/**
 * ECharts 的按需注册版：只有权益曲线用得到的图表与组件被注册。
 *
 * 这个模块**只能被动态 import**（见 AiEquityChart），这样它连同 echarts 都不在
 * 首屏静态依赖里；按需注册再把懒加载分块的体积压到全量 echarts 的一小截。
 * 新增图形能力时在这里补对应组件，漏注册的表现是图形静默不出现。
 */
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, GridComponent, TitleComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export const init = echarts.init;
export type ECharts = echarts.ECharts;
