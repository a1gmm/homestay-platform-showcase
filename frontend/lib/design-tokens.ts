export const tokens = {
  anyu: {
    color: {
      shell: "#FBF8F1",
      sand: "#F5F1EA",
      linen: "#E5DDCB",
      driftwood: "#A89680",
      stone: "#6B665B",
      ink: { default: "#2B2721", hover: "#1A1612" },
      sage: "#7B8578",
      clay: "#8A6E5A",
    },
    text: {
      primary: "#2B2721",
      secondary: "#5C5547",
      muted: "#7A6F5F",
      disabled: "#A89680",
    },
    radius: { none: 0, sm: 8, md: 12, lg: 16, pill: 999 },
    font: {
      serif:
        "'Cormorant Garamond', var(--font-noto-serif-sc), 'Noto Serif SC', 'Songti SC', 'Source Han Serif SC', 'STSong', serif",
      sans:
        "var(--font-inter), 'Inter', 'PingFang SC', 'HarmonyOS Sans SC', -apple-system, BlinkMacSystemFont, sans-serif",
    },
    transition: {
      breath: "cubic-bezier(0.2, 0.8, 0.2, 1)",
      breathSlow: "900ms cubic-bezier(0.2, 0.8, 0.2, 1)",
    },
  },

  color: {
    brand: {
      primary: "#2B2721",
      primaryHover: "#1A1612",
      primaryActive: "#1A1612",
      primarySoft: "#F5F1EA",
      primarySoftHover: "#E5DDCB",
    },
    gray: {
      50: "#FBF8F1",
      100: "#F5F1EA",
      200: "#E5DDCB",
      300: "#D8CFB9",
      400: "#A89680",
      500: "#6B665B",
      600: "#5C5547",
      700: "#3E3831",
      800: "#2B2721",
      900: "#1A1612",
    },
    status: {
      pending: "#8A6E5A",
      pendingSoft: "#F0E4D6",
      active: "#7B8578",
      activeSoft: "#E8ECE4",
      warn: "#8A6E5A",
      warnSoft: "#F0E4D6",
      info: "#6B665B",
      infoSoft: "#F5F1EA",
      neutral: "#6B665B",
      neutralSoft: "#F5F1EA",
    },
    bg: {
      page: "#FBF8F1",
      container: "#FBF8F1",
      subtle: "#F5F1EA",
      hover: "#F5F1EA",
      border: "#E5DDCB",
      borderSubtle: "#E5DDCB",
    },
    text: {
      primary: "#2B2721",
      secondary: "#5C5547",
      tertiary: "#7A6F5F",
      inverse: "#FBF8F1",
      brand: "#2B2721",
    },
  },
  radius: {
    xs: 4,
    sm: 8,
    md: 12,
    lg: 16,
    xl: 16,
    "2xl": 16,
    full: 999,
  },
  shadow: {
    none: "none",
    sm: "none",
    md: "none",
    lg: "none",
    xl: "none",
    focus: "0 0 0 2px rgba(43,39,33,.12)",
  },
  font: {
    family:
      "var(--font-inter), 'Inter', 'PingFang SC', 'HarmonyOS Sans SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
    serif:
      "'Cormorant Garamond', var(--font-noto-serif-sc), 'Noto Serif SC', 'Songti SC', 'Source Han Serif SC', 'STSong', serif",
    mono: "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",
    size: {
      xs: 12,
      sm: 13,
      base: 14,
      md: 15,
      lg: 16,
      xl: 18,
      "2xl": 24,
      "3xl": 32,
      "4xl": 40,
    },
    weight: {
      regular: 400,
      medium: 500,
      semibold: 500,
      bold: 500,
    },
    lineHeight: {
      tight: 1.4,
      normal: 1.6,
      relaxed: 1.8,
    },
  },
  space: {
    0: 0,
    1: 4,
    2: 8,
    3: 12,
    4: 16,
    5: 24,
    6: 32,
    7: 32,
    8: 48,
    9: 48,
    10: 64,
  },
  motion: {
    fast: "160ms cubic-bezier(0.2, 0.8, 0.2, 1)",
    base: "240ms cubic-bezier(0.2, 0.8, 0.2, 1)",
    slow: "420ms cubic-bezier(0.2, 0.8, 0.2, 1)",
    breath: "900ms cubic-bezier(0.2, 0.8, 0.2, 1)",
    ease: "cubic-bezier(0.2, 0.8, 0.2, 1)",
  },
  layout: {
    sidebarWidth: 232,
    sidebarCollapsedWidth: 64,
    headerHeight: 56,
    pagePaddingX: 24,
    pagePaddingY: 20,
    maxContentWidth: 1440,
    // 移动端
    bottomNavHeight: 64, // 底部导航高度（不含 safe-area）
    touchTarget: 44, // 最小触控目标 44×44（iOS HIG / Material）
    fabSize: 56, // 底部中央悬浮按钮直径
  },
  breakpoint: {
    sm: 640,
    md: 768,
    lg: 1024,
    xl: 1280,
    "2xl": 1536,
  },
} as const;

export type Tokens = typeof tokens;

export const statusColor = (key: keyof typeof tokens.color.status) =>
  tokens.color.status[key];

export const mqMobile = `@media (max-width: ${tokens.breakpoint.lg - 1}px)`;
export const mqDesktop = `@media (min-width: ${tokens.breakpoint.lg}px)`;
