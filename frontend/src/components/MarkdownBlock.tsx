import { memo } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

// 插件数组必须是模块级常量：内联字面量每次渲染都是新引用，memo 会直接失效。
const MARKDOWN_REMARK = [remarkGfm];
const MARKDOWN_REHYPE = [rehypeSanitize];

// react-markdown@10 的 Markdown() 每次渲染都重建 processor 并同步 parse+run，
// 自己不做 memo。流式期间每个 token 触发一次 setState，等于把**全部历史轮次**
// 重解析一遍——回答越长、会话越长就越卡，所以历史与流式块都走这个 memo 组件。
//
// 单独成文件是为了懒加载：markdown 全栈约 152 KB（raw），而 AI 抽屉默认关闭，
// 静态 import 会让每个用户首屏白背这一块。
const MarkdownBlock = memo(function MarkdownBlock({ source }: { source: string }) {
  return (
    <div className="ai-markdown">
      <ReactMarkdown remarkPlugins={MARKDOWN_REMARK} rehypePlugins={MARKDOWN_REHYPE}>
        {source}
      </ReactMarkdown>
    </div>
  );
});

export default MarkdownBlock;
