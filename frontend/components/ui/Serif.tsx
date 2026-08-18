import { CSSProperties, ElementType, ReactNode } from "react";

interface SerifProps {
  children: ReactNode;
  as?: ElementType;
  style?: CSSProperties;
  className?: string;
}

export function Serif({
  children,
  as: Tag = "span",
  style,
  className = "",
}: SerifProps) {
  return (
    <Tag className={`serif ${className}`.trim()} style={style}>
      {children}
    </Tag>
  );
}
