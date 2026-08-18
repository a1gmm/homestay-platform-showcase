import { CSSProperties, ReactNode } from "react";

interface EnLabelProps {
  children: ReactNode;
  style?: CSSProperties;
  className?: string;
}

export function EnLabel({ children, style, className = "" }: EnLabelProps) {
  return (
    <span className={`en-label ${className}`.trim()} style={style}>
      {children}
    </span>
  );
}
