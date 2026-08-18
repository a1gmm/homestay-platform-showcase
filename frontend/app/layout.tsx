import "@ant-design/v5-patch-for-react-19";
import type { Metadata, Viewport } from "next";
import { Inter, Cormorant_Garamond, Noto_Serif_SC } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { MOBILE_DEVICE_CLASS, MOBILE_UA_REGEX } from "@/lib/mobile-device";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
  variable: "--font-inter",
});

const cormorant = Cormorant_Garamond({
  subsets: ["latin"],
  weight: ["400", "500"],
  style: ["normal", "italic"],
  display: "swap",
  variable: "--font-cormorant",
});

const notoSerifSC = Noto_Serif_SC({
  weight: ["400", "500"],
  display: "swap",
  preload: false,
  variable: "--font-noto-serif-sc",
});

export const metadata: Metadata = {
  title: "成家民宿",
  description: "民宿/公寓运营管理系统",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  viewportFit: "cover",
};

// 挂载前（首帧绘制前）按 UA 打设备类，让「移动/桌面」结构完全由设备决定、
// 不随窗口宽度翻转。判据与类名从 lib/responsive.ts 的常量插值生成，与
// isMobileDevice() / useIsMobile() 在构造上就不可能漂移。
// suppressHydrationWarning：脚本给 <html> 加的类不算水合不一致，React 不会抹掉。
const DEVICE_CLASS_SCRIPT = `(function(){try{if(new RegExp(${JSON.stringify(
  MOBILE_UA_REGEX.source,
)},${JSON.stringify(MOBILE_UA_REGEX.flags)}).test(navigator.userAgent)){document.documentElement.classList.add(${JSON.stringify(
  MOBILE_DEVICE_CLASS,
)})}}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
      className={`${inter.variable} ${cormorant.variable} ${notoSerifSC.variable}`}
    >
      <body>
        <script dangerouslySetInnerHTML={{ __html: DEVICE_CLASS_SCRIPT }} />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
