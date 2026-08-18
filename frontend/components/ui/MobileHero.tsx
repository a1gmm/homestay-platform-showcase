import { CSSProperties, ReactNode } from "react";

interface MobileHeroProps {
  children: ReactNode;
  padding?: string | number;
  style?: CSSProperties;
}

/**
 * 移动端页面顶部 hero 区 —— 墨底米字,替代旧的蓝紫渐变。
 * 配合品牌规范,子内容里数字/品牌名用 .serif,辅助标签用 <EnLabel />。
 */
export function MobileHero({
  children,
  padding = "32px 20px 28px",
  style,
}: MobileHeroProps) {
  return (
    <div
      style={{
        background: "#2B2721",
        color: "#FBF8F1",
        padding,
        paddingTop: `calc(${
          typeof padding === "number" ? `${padding}px` : padding.split(" ")[0]
        } + env(safe-area-inset-top, 0px))`,
        ...style,
      }}
    >
      {children}
    </div>
  );
}
