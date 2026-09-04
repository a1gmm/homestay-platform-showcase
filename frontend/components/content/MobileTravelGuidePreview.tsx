"use client";

import type { TravelGuideDraft } from "@/features/content/types";
import type { ContentPreviewWidth } from "./ContentEditorShell";
import styles from "./content-center.module.css";

export const TRAVEL_CATEGORY_OPTIONS = [
  ["must_see", "必去景点"],
  ["dining", "吃饭推荐"],
  ["family", "亲子游玩"],
  ["transport_parking", "交通停车"],
  ["shopping", "购物便利"],
  ["rainy_day", "雨天安排"],
] as const;

export function MobileTravelGuidePreview({
  draft,
  width = 390,
}: {
  draft: TravelGuideDraft;
  width?: ContentPreviewWidth;
}) {
  return (
    <article className={styles.stayGuidePhone} data-testid="travel-guide-phone-preview" style={{ width, maxWidth: "100%" }}>
      <header className={styles.stayGuidePhoneHeader}>
        <p>观海居住客服务</p>
        <h1>{draft.title || "灵山湾旅游攻略"}</h1>
        <div>{draft.intro}</div>
        <small>推荐为公开通用建议，出发前请再次确认营业时间和现场开放情况。</small>
      </header>
      <div className={styles.stayGuidePhoneBody}>
        {TRAVEL_CATEGORY_OPTIONS.map(([category, label]) => {
          const items = draft.recommendations.filter((item) => item.visible && item.category === category);
          if (!items.length) return null;
          return (
            <section key={category}>
              <h2>{label}</h2>
              <ol className={styles.stayGuidePhoneFaq}>
                {items.map((item) => (
                  <li key={item.id}>
                    <strong>{item.name}</strong>
                    <span>{item.reason}</span>
                    {item.approximateLocation ? <span>大概位置：{item.approximateLocation}</span> : null}
                    {item.suggestedDuration ? <span>建议时长：{item.suggestedDuration}</span> : null}
                    <span>适合：{item.audiences.join("、")}</span>
                    {item.image ? <span className={styles.stayGuideImagePlaceholder}>已设置公开图片</span> : null}
                  </li>
                ))}
              </ol>
            </section>
          );
        })}
      </div>
    </article>
  );
}
