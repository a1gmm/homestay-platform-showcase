import type { CSSProperties } from "react";

/**
 * 竖排单字根因修复工具。
 *
 * 根因：flex / grid 子项默认 min-width:auto，不会收缩到内容宽度以下；当容器不够宽时，
 * 中文文本被迫一字一行（竖排单字）。所有"可能被压缩、需要截断"的文本节点必须显式
 * 设置 min-width:0 + 溢出处理。详见 DESIGN.md「移动端文本截断」。
 */

/** 单行截断 + 省略号。用于标题、名称等不应换行的文本。 */
export const ellipsis: CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

/** 多行截断到 n 行 + 省略号。用于描述、地址等允许多行但需封顶的文本。 */
export function clamp(lines: number): CSSProperties {
  return {
    minWidth: 0,
    overflow: "hidden",
    display: "-webkit-box",
    WebkitBoxOrient: "vertical",
    WebkitLineClamp: lines,
  };
}

/**
 * flex 子项收缩兜底。铺在包裹可截断文本的 flex 子项上，允许它收缩到容器宽度，
 * 配合内部文本的 ellipsis/clamp 使用。这是消灭竖排单字的关键一招。
 */
export const flexShrinkText: CSSProperties = {
  minWidth: 0,
  flex: "1 1 auto",
};
