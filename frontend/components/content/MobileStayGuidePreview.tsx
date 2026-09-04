"use client";

import type { StayGuideDraft } from "@/features/content/types";
import type { ContentPreviewWidth } from "./ContentEditorShell";
import styles from "./content-center.module.css";

const SECTION_LABELS = {
  arrivalDeparture: "入住与退房时间",
  parking: "停车说明",
  wifiAndDevices: "Wi-Fi 与常用设备",
  houseRules: "住宿规则",
  checkOut: "退房说明",
  support: "联系管家",
} as const;

export function MobileStayGuidePreview({
  draft,
  width,
}: {
  draft: StayGuideDraft;
  width: ContentPreviewWidth;
}) {
  return (
    <article
      className={styles.stayGuidePhone}
      data-testid="stay-guide-phone-preview"
      style={{ width, maxWidth: "100%" }}
    >
      <header className={styles.stayGuidePhoneHeader}>
        <p>观海居住客服务</p>
        <h1>{draft.title || "通用入住说明"}</h1>
        <div>{draft.intro}</div>
      </header>
      <div className={styles.stayGuidePhoneBody}>
        {(Object.keys(SECTION_LABELS) as Array<keyof typeof SECTION_LABELS>).map((key) => {
          const section = draft.sections[key];
          if (!section.visible) return null;
          return (
            <section key={key}>
              <h2>{SECTION_LABELS[key]}</h2>
              <p>{section.summary}</p>
              {section.details ? <div>{section.details}</div> : null}
              {section.image ? <span className={styles.stayGuideImagePlaceholder}>已设置公开图片</span> : null}
            </section>
          );
        })}
        {draft.sections.faq.visible ? (
          <section>
            <h2>常见问题</h2>
            <p>{draft.sections.faq.summary}</p>
            {draft.sections.faq.details ? <div>{draft.sections.faq.details}</div> : null}
            {draft.sections.faq.image ? <span className={styles.stayGuideImagePlaceholder}>已设置公开图片</span> : null}
            <ol className={styles.stayGuidePhoneFaq}>
              {draft.sections.faq.items.map((item, index) => (
                <li key={`${index}-${item.question}`}>
                  <strong>{item.question}</strong>
                  <span>{item.answer}</span>
                </li>
              ))}
            </ol>
          </section>
        ) : null}
      </div>
    </article>
  );
}
