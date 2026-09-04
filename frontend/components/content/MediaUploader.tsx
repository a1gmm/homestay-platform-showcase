"use client";

import { useEffect, useRef, useState } from "react";
import { UploadOutlined } from "@ant-design/icons";
import styles from "./owner-content.module.css";

export interface UploadedContentMedia {
  mediaId: string;
  mediaType: "image" | "video";
  mimeType: string;
  sizeBytes: number;
  sha256: string;
}

export interface MediaUploadProgress {
  transfer(percent: number): void;
  processing(label: string): void;
}

export type MediaUploadHandler<T> = (
  file: File,
  progress: MediaUploadProgress,
  signal: AbortSignal,
) => Promise<T>;

export interface MediaUploaderProps<T = UploadedContentMedia> {
  kind: "image" | "video";
  onUpload: MediaUploadHandler<T>;
  onUploaded: (media: T) => void;
  onActiveChange?: (active: boolean) => void;
  buttonLabel?: string;
  disabled?: boolean;
}

const limits = {
  image: { accept: "image/jpeg,image/png,image/webp", max: 8 * 1024 * 1024, formats: "JPG、PNG 或 WebP，最大 8MB" },
  video: { accept: "video/mp4", max: 80 * 1024 * 1024, formats: "MP4，最大 80MB" },
};

export function MediaUploader<T = UploadedContentMedia>({ kind, onUpload, onUploaded, onActiveChange, buttonLabel, disabled }: MediaUploaderProps<T>) {
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const [isUploading, setIsUploading] = useState(false);
  const [transfer, setTransfer] = useState<number | null>(null);
  const [processing, setProcessing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const label = buttonLabel || (kind === "video" ? "上传工作过程视频" : "上传图片");

  useEffect(() => () => {
    mountedRef.current = false;
    abortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!isUploading) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [isUploading]);

  const chooseFile = async (file: File | undefined) => {
    if (!file) return;
    const config = limits[kind];
    const allowed = config.accept.split(",").includes(file.type);
    if (!allowed) {
      setError(`文件格式不支持。请选择${config.formats}`);
      return;
    }
    if (file.size > config.max) {
      setError(`文件过大。请选择${config.formats}`);
      return;
    }
    setError(null);
    setIsUploading(true);
    const controller = new AbortController();
    const activeChange = onActiveChange;
    abortRef.current = controller;
    activeChange?.(true);
    setTransfer(0);
    setProcessing(kind === "video" ? "等待服务端校验" : null);
    try {
      const uploaded = await onUpload(file, {
        transfer: (percent) => {
          if (mountedRef.current && !controller.signal.aborted) {
            setTransfer(Math.max(0, Math.min(100, Math.round(percent))));
          }
        },
        processing: (value) => {
          if (mountedRef.current && !controller.signal.aborted) setProcessing(value);
        },
      }, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      setTransfer(100);
      setProcessing("服务端处理完成");
      onUploaded(uploaded);
    } catch (uploadError) {
      if (!mountedRef.current) return;
      setError(controller.signal.aborted
        ? "上传已取消"
        : uploadError instanceof Error ? uploadError.message : "上传失败，请重试");
      setProcessing(null);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      if (!mountedRef.current) return;
      activeChange?.(false);
      setIsUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <div className={styles.uploader}>
      <input
        ref={inputRef}
        className={styles.visuallyHidden}
        type="file"
        accept={limits[kind].accept}
        disabled={disabled || isUploading}
        aria-label={kind === "video" ? "选择工作过程视频" : `选择${label.replace(/^上传/, "")}`}
        onChange={(event) => void chooseFile(event.target.files?.[0])}
      />
      <button
        type="button"
        className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`}
        disabled={disabled || isUploading}
        onClick={() => inputRef.current?.click()}
      >
        <UploadOutlined aria-hidden="true" /> {isUploading ? "上传中" : label}
      </button>
      {isUploading ? (
        <button
          type="button"
          className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`}
          onClick={() => abortRef.current?.abort()}
          aria-label={`取消${label}`}
        >取消上传</button>
      ) : null}
      <span className={styles.uploadHint}>{limits[kind].formats}</span>
      {transfer !== null ? (
        <div className={styles.uploadStage}>
          <span>传输 {transfer}%</span>
          <progress aria-label={`${kind === "video" ? "视频" : "图片"}传输进度`} max={100} value={transfer} />
        </div>
      ) : null}
      {processing ? (
        <div className={styles.uploadStage} role="status">
          <span>{processing}</span>
          <span className={styles.statusMark} aria-hidden="true">{processing === "服务端处理完成" ? "✓" : "…"}</span>
        </div>
      ) : null}
      {error ? <p className={styles.fieldError} role="alert">{error}</p> : null}
    </div>
  );
}
