"use client";

import { useEffect, useRef, useState } from "react";
import type { OwnerDraftPreview, OwnerManifest } from "@/features/content/owner/types";
import styles from "./owner-content.module.css";

export type OwnerPreviewWidth = 375 | 390 | 430;

function isManifest(preview: OwnerDraftPreview): preview is OwnerManifest {
  return "schema" in preview;
}

export function MobileOwnerPreview({
  manifest,
  width,
  onExpired,
}: {
  manifest: OwnerDraftPreview | null;
  width: OwnerPreviewWidth;
  onExpired: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const expiredRef = useRef(false);
  const manifestUrlGeneration = manifest && isManifest(manifest)
    ? [
        manifest.version,
        manifest.hero.url,
        ...manifest.cases.flatMap((item) => [
          item.hero.url,
          ...item.images.flatMap((image) => [image.thumbnail.url, image.display.url]),
        ]),
        manifest.video?.url || "",
        manifest.video?.poster.url || "",
      ].join("|")
    : null;

  useEffect(() => {
    expiredRef.current = false;
  }, [manifestUrlGeneration]);

  if (!manifest) {
    return (
      <div className={styles.previewEmpty} role="status">
        <h3>等待生成预览</h3>
        <p>点击“查看手机效果”，系统会先保存修改，再生成与小程序一致的预览。</p>
      </div>
    );
  }
  if (!isManifest(manifest) || manifest.cases.length === 0) {
    return (
      <div className={styles.previewEmpty} role="status">
        <h3>草稿还没有可预览内容</h3>
        <p>新增真实房源展示并上传首图后，再查看手机效果。</p>
      </div>
    );
  }

  const imageExpired = () => {
    if (expiredRef.current) return;
    expiredRef.current = true;
    onExpired();
  };
  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (playing) {
      video.pause();
      setPlaying(false);
      return;
    }
    try {
      const result = video.play();
      result?.catch(() => setPlaying(false));
      setPlaying(true);
    } catch {
      setPlaying(false);
    }
  };

  return (
    <article
      className={styles.phone}
      style={{ width: `${width}px` }}
      aria-label={`${width} 像素手机预览`}
    >
      <header className={styles.phoneBar}><span>9:41</span><strong>观海居 · 业主托管</strong><span aria-hidden="true">••</span></header>
      <section className={styles.phoneHero}>
        {/* eslint-disable-next-line @next/next/no-img-element -- signed preview URLs are deliberately ephemeral */}
        <img src={manifest.hero.url} alt={manifest.hero.alt} onError={imageExpired} />
        <div className={styles.phoneHeroCopy}>
          <span>灵山湾本地房屋托管</span>
          <h2>房子交给<br />真正到场的人</h2>
        </div>
      </section>
      <section className={styles.phoneCases} aria-labelledby="preview-cases-title">
        <h2 id="preview-cases-title">真实房源展示</h2>
        <p>不是样板间，是正在照看的房子。</p>
        {manifest.cases.map((ownerCase, index) => (
          <article className={styles.phoneCase} key={ownerCase.id}>
            {/* eslint-disable-next-line @next/next/no-img-element -- signed preview URLs are deliberately ephemeral */}
            <img src={ownerCase.hero.url} alt={ownerCase.hero.alt} onError={imageExpired} />
            <div><strong>{ownerCase.title}</strong><p>{ownerCase.summary}</p></div>
            {ownerCase.images.map((image) => (
              // eslint-disable-next-line @next/next/no-img-element -- signed preview URLs are deliberately ephemeral
              <img key={image.display.sha256} src={image.display.url} alt={image.alt} onError={imageExpired} />
            ))}
            <small>房源记录 {String(index + 1).padStart(2, "0")}</small>
          </article>
        ))}
      </section>
      {manifest.video ? (
        <section className={styles.phoneVideo} aria-label="工作过程视频">
          <video
            ref={videoRef}
            src={manifest.video.url}
            poster={manifest.video.poster.url}
            aria-label={manifest.video.alt}
            muted={muted}
            autoPlay={false}
            playsInline
            onEnded={() => setPlaying(false)}
            onError={imageExpired}
          />
          <div className={styles.videoControls}>
            <button type="button" className={`touchTarget ${styles.touchTarget}`} onClick={togglePlayback} aria-label={`${playing ? "暂停" : "播放"}工作过程视频`}>
              {playing ? "暂停" : "播放"}
            </button>
            <button type="button" className={`touchTarget ${styles.touchTarget}`} onClick={() => setMuted((value) => !value)} aria-label={`${muted ? "开启" : "关闭"}工作过程视频声音`}>
              {muted ? "静音 · 开声" : "已开声 · 静音"}
            </button>
          </div>
        </section>
      ) : null}
      <section className={styles.phoneCta}>
        <h2>先聊聊你的房子</h2>
        <p>电话与留资入口由小程序固定页面提供，弱网下仍可继续操作。</p>
      </section>
    </article>
  );
}
